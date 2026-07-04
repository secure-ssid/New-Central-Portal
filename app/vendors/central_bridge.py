"""Bridge to centralmcp tools — async wrappers for FastAPI.

Requires these env vars (set in docker-compose.yml):
  PYTHONPATH=/centralmcp
  CREDS_PATH=/centralmcp/config/credentials.yaml
  CLASSIC_CENTRAL_BASE_URL, CLASSIC_CENTRAL_CLIENT_ID, etc. (Classic Central)

RAG doc search delegates to centralmcp's LanceDB backend by default
(CENTRALMCP_RAG_BACKEND=lancedb). Set CENTRALMCP_RAG_BACKEND=redis plus
OLLAMA_URL when using the optional Redis Stack deployment.
"""
import asyncio
import importlib
import inspect
import json
import logging
import os
import time
import threading
from functools import partial
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ── Classic Central Client ────────────────────────────────────────────────────

class ClassicCentralClient:
    """Lightweight OAuth2 client for the Classic Central gateway."""

    def __init__(self):
        missing = [
            v for v in (
                "CLASSIC_CENTRAL_BASE_URL",
                "CLASSIC_CENTRAL_CLIENT_ID",
                "CLASSIC_CENTRAL_CLIENT_SECRET",
            )
            if not os.environ.get(v)
        ]
        if missing:
            raise RuntimeError(
                "Classic Central is not configured — missing env vars: "
                + ", ".join(missing)
            )
        self.base_url = os.environ["CLASSIC_CENTRAL_BASE_URL"].rstrip("/")
        self.client_id = os.environ["CLASSIC_CENTRAL_CLIENT_ID"]
        self.client_secret = os.environ["CLASSIC_CENTRAL_CLIENT_SECRET"]
        self._access_token = os.environ.get("CLASSIC_CENTRAL_ACCESS_TOKEN", "")
        self._refresh_token = os.environ.get("CLASSIC_CENTRAL_REFRESH_TOKEN", "")
        self._expires_at: float = time.time() + 7000  # assume ~2h from startup
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        """Refresh the OAuth2 access token."""
        if not self._refresh_token:
            raise RuntimeError(
                "Classic Central access token expired and no "
                "CLASSIC_CENTRAL_REFRESH_TOKEN is configured."
            )
        r = requests.post(
            f"{self.base_url}/oauth2/token",
            json={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._refresh_token,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + data.get("expires_in", 7200) - 120

    def _ensure_token(self) -> str:
        with self._lock:
            if time.time() >= self._expires_at:
                self._refresh()
            return self._access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        return requests.get(f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=30)

    def post(self, path: str, payload: dict | None = None) -> requests.Response:
        return requests.post(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=30)


_classic_client: ClassicCentralClient | None = None


def get_classic_client() -> ClassicCentralClient:
    global _classic_client
    if _classic_client is None:
        _classic_client = ClassicCentralClient()
    return _classic_client


async def _run(fn, *args, **kwargs) -> Any:
    """Run a centralmcp function — await coroutines, thread-pool sync callables."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


def _resolve_troubleshoot_type(serial: str, device_type: str) -> str | None:
    """Map portal device types to centralmcp troubleshoot URL segments."""
    from mcp_servers.shared import device_type_for_troubleshoot

    portal_dt = (device_type or "").lower()
    # Generic "switch" from aruba_central normalization — disambiguate via inventory.
    if portal_dt in ("switch", "unknown", ""):
        return device_type_for_troubleshoot(serial, None)
    return device_type_for_troubleshoot(serial, device_type)


def _ops_error(message: str) -> dict:
    return {"status": None, "errors": [message]}


def _unwrap(result: list | dict | None) -> list[dict]:
    """Flatten paginated centralmcp responses to a plain list."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        items = result.get("items", [])
        return items if isinstance(items, list) else []
    if result is not None:
        logger.warning("Unexpected centralmcp response shape: %s", type(result).__name__)
    return []


async def _fetch_paginated(fetch_fn, *, limit: int = 200, max_items: int = 1000) -> list[dict]:
    """Page through a centralmcp list tool until exhausted or max_items reached."""
    collected: list[dict] = []
    offset = 0
    while len(collected) < max_items:
        page = _unwrap(await fetch_fn(limit=limit, offset=offset))
        if not page:
            break
        collected.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return collected[:max_items]


# ── Sites ─────────────────────────────────────────────────────────────────────

async def get_sites(limit: int = 100) -> list[dict]:
    from mcp_servers.monitoring import list_sites
    return _unwrap(await _run(list_sites, limit=limit))


async def find_site(name: str) -> dict | None:
    from mcp_servers.monitoring import get_site
    return await _run(get_site, name=name)


# ── Devices ──────────────────────────────────────────────────────────────────

async def get_devices(
    device_type: str | None = None,
    site_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    from mcp_servers.monitoring import list_devices
    kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
    if device_type:
        kwargs["device_type"] = device_type
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_devices, **kwargs))


async def get_all_devices(limit_per_page: int = 200, max_items: int = 1000) -> list[dict]:
    async def _page(**kwargs):
        return await get_devices(**kwargs)
    return await _fetch_paginated(_page, limit=limit_per_page, max_items=max_items)


async def get_device(serial: str) -> dict | None:
    from mcp_servers.monitoring import find_device
    return await _run(find_device, serial_number=serial)


async def get_switch_ports(serial: str) -> list[dict]:
    from mcp_servers.monitoring import list_switch_ports
    result = await _run(list_switch_ports, serial)
    return result.get("interfaces", []) if isinstance(result, dict) else []


# In-module TTL cache for switch-port lookups used by uplink resolution.
# find_device_uplink scans every switch's ports per call; caching the port
# tables for a short window makes repeated uplink lookups cheap.
_PORTS_CACHE_TTL_SECONDS = 60.0
_ports_cache: dict[str, tuple[float, list[dict]]] = {}
_switches_cache: tuple[float, list[dict]] | None = None
_SWITCHES_CACHE_TTL_SECONDS = 60.0


async def _get_switches_cached() -> list[dict]:
    """List switch devices with a short TTL cache (uplink resolution only)."""
    global _switches_cache
    now = time.monotonic()
    if _switches_cache is not None and (now - _switches_cache[0]) < _SWITCHES_CACHE_TTL_SECONDS:
        return _switches_cache[1]
    from mcp_servers.monitoring import list_devices
    all_devices = _unwrap(await _run(list_devices, limit=200))
    switches = [
        d for d in all_devices
        if isinstance(d, dict)
        and (d.get("deviceType") or "").upper() in ("SWITCH", "AOS_S", "AOS-S", "CX", "AOS_CX")
    ]
    _switches_cache = (time.monotonic(), switches)
    return switches


async def _get_switch_ports_cached(serial: str) -> list[dict]:
    """get_switch_ports with a short TTL cache (uplink resolution only)."""
    now = time.monotonic()
    entry = _ports_cache.get(serial)
    if entry is not None and (now - entry[0]) < _PORTS_CACHE_TTL_SECONDS:
        return entry[1]
    ports = await get_switch_ports(serial)
    _ports_cache[serial] = (time.monotonic(), ports)
    # Opportunistic pruning so the cache can't grow unbounded.
    if len(_ports_cache) > 256:
        cutoff = time.monotonic() - _PORTS_CACHE_TTL_SECONDS
        for key in [k for k, (ts, _) in _ports_cache.items() if ts < cutoff]:
            _ports_cache.pop(key, None)
    return ports


async def find_device_uplink(device_serial: str) -> dict | None:
    """Return the switch + port that an AP/device uplinks through."""
    switches = await _get_switches_cached()
    for sw in switches:
        sw_serial = sw.get("serialNumber") or sw.get("serial") or ""
        if not sw_serial:
            continue
        try:
            ports = await _get_switch_ports_cached(sw_serial)
            for port in ports:
                if isinstance(port, dict) and port.get("neighbourSerial") == device_serial:
                    return {
                        "switch_serial": sw_serial,
                        "switch_name": sw.get("deviceName") or sw.get("name") or sw_serial,
                        "port": port.get("name") or port.get("id") or "",
                    }
        except Exception as exc:
            logger.warning(
                "Uplink scan: failed to list ports for switch %s: %s", sw_serial, exc
            )
            continue
    return None


async def get_device_events(serial: str, hours: int = 24, limit: int = 20) -> list[dict]:
    from mcp_servers.monitoring import list_events
    result = await _run(list_events, serial_number=serial, hours=hours, limit=limit)
    return _unwrap(result) if isinstance(result, dict) else (result if isinstance(result, list) else [])


async def get_device_health(serial: str) -> dict:
    from mcp_servers.monitoring import get_device_health as _health
    return await _run(_health, serial_number=serial)


async def get_lldp_neighbors(serial: str) -> dict:
    from mcp_servers.ops import get_lldp_neighbors as _lldp
    return await _run(_lldp, serial)


async def get_site_health_summary(site_id: str | None = None, site_name: str | None = None) -> dict:
    from mcp_servers.monitoring import get_site_health_summary as _summary
    kwargs: dict = {}
    if site_id:
        kwargs["site_id"] = site_id
    if site_name:
        kwargs["site_name"] = site_name
    return await _run(_summary, **kwargs)


async def get_tenant_health() -> dict:
    from mcp_servers.monitoring import get_tenant_health as _health
    return await _run(_health)


async def get_client_details(mac_address: str) -> dict:
    from mcp_servers.monitoring import get_client_details
    return await _run(get_client_details, mac_address=mac_address)


async def locate_client(mac_address: str) -> dict:
    from mcp_servers.monitoring import locate_client as _locate
    return await _run(_locate, mac_address=mac_address)


async def get_client_roaming_history(mac_address: str, hours: int = 24) -> dict:
    from mcp_servers.monitoring import get_client_roaming_history as _roam
    return await _run(_roam, mac_address=mac_address, hours=hours)


async def list_active_alerts(limit: int = 50) -> list[dict]:
    from mcp_servers.monitoring import list_active_alerts
    result = await _run(list_active_alerts, limit=limit)
    if isinstance(result, dict):
        return result.get("items", result.get("alerts", [])) or []
    return result if isinstance(result, list) else []


async def run_traceroute(serial: str, device_type: str, destination: str) -> dict:
    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "cx":
        from mcp_servers.ops import cx_traceroute
        return await _run(cx_traceroute, serial, destination)
    if dtype == "aos-s":
        from mcp_servers.ops import aos_s_traceroute
        return await _run(aos_s_traceroute, serial, destination)
    return _ops_error(f"Traceroute is not supported for this device type on {serial}.")


async def get_switch_port_errors(serial: str, interface: str | None = None) -> dict:
    from mcp_servers.ops import get_switch_port_errors as _fn
    return await _run(_fn, serial, interface=interface)


async def find_mac_on_switch(serial: str, mac_address: str) -> dict:
    from mcp_servers.ops import find_mac_on_switch as _fn
    return await _run(_fn, serial, mac_address)


async def get_cx_mac_table(serial: str, interface: str | None = None) -> dict:
    from mcp_servers.ops import get_cx_mac_table as _fn
    return await _run(_fn, serial, interface=interface)


async def get_wireless_metrics(serial: str) -> dict:
    from mcp_servers.monitoring import get_wireless_metrics as _fn
    return await _run(_fn, serial)


async def get_ap_radios(serial: str) -> dict:
    from mcp_servers.monitoring import get_ap_radios as _fn
    return await _run(_fn, serial)


async def get_channel_utilization(serial: str) -> dict:
    from mcp_servers.monitoring import get_channel_utilization as _fn
    return await _run(_fn, serial)


async def get_ap_rf_neighbors(serial: str) -> list[dict]:
    """Co-channel / RF neighbor APs for an access point (best-effort)."""
    try:
        from mcp_servers.monitoring import get_ap_neighbors as _fn
        result = await _run(_fn, serial)
    except Exception:
        result = await invoke_tool_router(
            "get_ap_neighbors", {"serial": serial, "serialNumber": serial}
        )
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        if result.get("error"):
            return []
        items = result.get("neighbors") or result.get("items") or result.get("aps") or []
        return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []
    return []


async def detect_client_flapping(serial: str, hours: int = 24) -> dict:
    from mcp_servers.monitoring import detect_client_flapping as _fn
    return await _run(_fn, serial, hours=hours)


async def detect_ssh_brute_force(serial: str, hours: int = 24) -> dict:
    from mcp_servers.monitoring import detect_ssh_brute_force as _fn
    return await _run(_fn, serial, hours=hours)


async def list_wlans(limit: int = 50) -> list[dict]:
    from mcp_servers.config import list_ssids
    result = await _run(list_ssids, limit=limit)
    return _unwrap(result) if not isinstance(result, list) else result


async def get_firmware_compliance(limit: int = 50) -> dict:
    from mcp_servers.config import get_firmware_compliance as _fn
    return await _run(_fn, limit=limit)


async def get_device_running_config(serial: str) -> dict:
    from mcp_servers.config import get_device_running_config as _fn
    return await _run(_fn, serial)


async def list_mac_registrations(limit: int = 50, offset: int = 0) -> list[dict]:
    from mcp_servers.nac import list_mac_registrations as _fn
    result = await _run(_fn, limit=limit, offset=offset)
    return _unwrap(result) if isinstance(result, dict) else (result if isinstance(result, list) else [])


async def list_glp_service_offers(limit: int = 50) -> list[dict]:
    from mcp_servers.glp import list_glp_service_offers
    result = await _run(list_glp_service_offers, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


async def invoke_tool_router(tool_name: str, params: dict) -> dict:
    """Fallback dispatch via centralmcp tool_router when not in _TOOL_MAP."""
    try:
        from mcp_servers.tool_router import invoke_read_tool
        return await _run(invoke_read_tool, tool_name=tool_name, params=params)
    except Exception as exc:
        return {"error": str(exc)}


# ── Clients ──────────────────────────────────────────────────────────────────

async def get_clients(
    site_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    from mcp_servers.monitoring import list_clients
    kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_clients, **kwargs))


async def get_all_clients(limit_per_page: int = 200, max_items: int = 1000) -> list[dict]:
    async def _page(**kwargs):
        return await get_clients(**kwargs)
    return await _fetch_paginated(_page, limit=limit_per_page, max_items=max_items)


async def find_client(mac_or_ip: str) -> dict | None:
    from mcp_servers.monitoring import find_client as _find
    return await _run(_find, mac_or_ip=mac_or_ip)


# ── Alerts ────────────────────────────────────────────────────────────────────

async def get_alerts(site_id: str | None = None, limit: int = 50) -> list[dict]:
    from mcp_servers.monitoring import list_alerts
    kwargs: dict[str, Any] = {"limit": limit}
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_alerts, **kwargs))


# ── Device Groups & Sites (Classic Central) ──────────────────────────────────

async def get_device_groups() -> list[dict]:
    """List groups via Classic Central API."""
    def _fetch():
        c = get_classic_client()
        r = c.get("/configuration/v2/groups", params={"limit": 50, "offset": 0})
        r.raise_for_status()
        data = r.json()
        # Classic returns {"data": [["group1"],["group2"]], "total": N}
        names = []
        for row in data.get("data", []) if isinstance(data, dict) else []:
            if isinstance(row, (list, tuple)) and row:
                names.append(row[0])
            elif isinstance(row, str) and row:
                names.append(row)
        return [{"groupName": n} for n in names]
    return await _run(_fetch)


async def get_classic_sites() -> list[dict]:
    """List sites via Classic Central API — returns site_id (int), site_name, etc."""
    def _fetch():
        c = get_classic_client()
        r = c.get("/central/v2/sites", params={"limit": 100, "offset": 0})
        r.raise_for_status()
        data = r.json()
        sites = data.get("sites", []) if isinstance(data, dict) else []
        return sites if isinstance(sites, list) else []
    return await _run(_fetch)


async def get_central_sites() -> list[dict]:
    """Alias for get_classic_sites."""
    return await get_classic_sites()


async def move_device_to_group(group_name: str, serials: list[str]) -> dict:
    """Move devices to a group via Classic Central API."""
    def _move():
        c = get_classic_client()
        r = c.post("/configuration/v1/devices/move", payload={"group": group_name, "serials": serials})
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"status_code": r.status_code, "response": body}
    return await _run(_move)


async def assign_device_to_site(site_id: int, serials: list[str], device_type: str = "IAP") -> dict:
    """Associate devices with a site via Classic Central API."""
    dtype_map = {"access_point": "IAP", "gateway": "CONTROLLER", "switch": "CX"}
    dt = dtype_map.get(device_type.lower(), device_type.upper()) if device_type else "IAP"
    def _assign():
        c = get_classic_client()
        r = c.post("/central/v2/sites/associations", payload={
            "site_id": int(site_id),
            "device_ids": serials,
            "device_type": dt,
        })
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"status_code": r.status_code, "response": body}
    return await _run(_assign)


# Keep old aliases that used centralmcp for backward compat
async def add_device_to_group(scope_id: str, serial_numbers: list[str]) -> dict:
    """Deprecated — use move_device_to_group instead."""
    return await move_device_to_group(scope_id, serial_numbers)


# ── Ops ───────────────────────────────────────────────────────────────────────

async def run_show(serial: str, device_type: str, commands: list[str]) -> dict:
    """Run show commands — picks the right function by device type."""
    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "aps":
        return _ops_error(
            "Show commands are not supported on Access Points via the Central troubleshooting API."
        )
    if dtype == "cx":
        from mcp_servers.ops import cx_show
        return await _run(cx_show, serial, commands)
    if dtype == "aos-s":
        from mcp_servers.ops import aos_s_show
        return await _run(aos_s_show, serial, commands)
    if dtype == "gateways":
        from mcp_servers.ops import gateway_show
        return await _run(gateway_show, serial, commands)
    return _ops_error(f"Could not determine device type for show commands on {serial}.")


async def run_ping(serial: str, device_type: str, destination: str, count: int = 5) -> dict:
    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "aps":
        return _ops_error(
            "Ping is not supported on Access Points via the Central troubleshooting API."
        )
    if dtype == "cx":
        from mcp_servers.ops import cx_ping
        return await _run(cx_ping, serial, destination, count=count)
    if dtype == "aos-s":
        from mcp_servers.ops import aos_s_ping
        return await _run(aos_s_ping, serial, destination)
    if dtype == "gateways":
        return _ops_error("Ping is not supported on gateways.")
    return _ops_error(f"Could not determine device type for ping on {serial}.")


async def run_reboot(serial: str, device_type: str) -> dict:
    """Reboot a device via Central troubleshooting API (bypasses MCP elicitation)."""
    from mcp_servers.shared import _AOS_S_BASE, compact_http_error, get_client

    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "aps":
        endpoint = f"/network-troubleshooting/v1alpha1/aps/{serial}/reboot"
        reboot_type = "AP"
    elif dtype == "cx":
        endpoint = f"/network-troubleshooting/v1alpha1/cx/{serial}/reboot"
        reboot_type = "CX"
    elif dtype == "aos-s":
        endpoint = f"{_AOS_S_BASE}/{serial}/reboot"
        reboot_type = "AOS-S"
    elif dtype == "gateways":
        endpoint = f"/network-troubleshooting/v1alpha1/gateways/{serial}/reboot"
        reboot_type = "GATEWAY"
    else:
        return {
            "status": "failed",
            "serial_number": serial,
            "device_type": device_type,
            "response": None,
            "errors": [f"Could not determine device type for reboot on {serial}."],
        }

    errors: list[str] = []
    client = get_client()
    try:
        response = await client._arequest("POST", endpoint, json={})
        if response.status_code not in (200, 201, 202):
            errors.append(compact_http_error(response))
            return {
                "status": "failed",
                "serial_number": serial,
                "device_type": reboot_type,
                "response": None,
                "errors": errors,
            }
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {
            "status": "submitted",
            "serial_number": serial,
            "device_type": reboot_type,
            "response": resp_body,
            "errors": errors,
        }
    except Exception as exc:
        errors.append(str(exc))
        return {
            "status": "failed",
            "serial_number": serial,
            "device_type": reboot_type,
            "response": None,
            "errors": errors,
        }


# ── GreenLake Platform (GLP) ──────────────────────────────────────────────────

async def get_glp_devices(limit: int = 200) -> list[dict]:
    from mcp_servers.glp import list_glp_devices
    result = await _run(list_glp_devices, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


async def get_glp_subscriptions(limit: int = 200) -> list[dict]:
    from mcp_servers.glp import list_glp_subscriptions
    result = await _run(list_glp_subscriptions, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


async def get_glp_users(limit: int = 300) -> list[dict]:
    from mcp_servers.glp import list_glp_users
    result = await _run(list_glp_users, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


async def get_glp_audit_logs(limit: int = 100, category: str | None = None) -> list[dict]:
    from mcp_servers.glp import list_glp_audit_logs
    kwargs: dict[str, Any] = {"limit": limit}
    if category:
        kwargs["category"] = category
    result = await _run(list_glp_audit_logs, **kwargs)
    return result.get("items", []) if isinstance(result, dict) else []


async def assign_glp_subscription(serial_number: str, subscription_id: str) -> dict:
    """Assign a subscription UUID to a device via GLPClient.

    Sends the PATCH and returns immediately once accepted (202).
    Does NOT poll the async-operation — the page reload will show the result.
    """
    from mcp_servers.glp import get_glp_client

    def _do_assign():
        glp = get_glp_client()
        device_id = glp.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(f"Could not resolve serial {serial_number!r} to a GLP device ID.")
        body = {"subscription": [{"id": subscription_id}]}
        resp = glp._client._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"GLP PATCH returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "accepted", "http": resp.status_code}

    return await _run(_do_assign)


async def unassign_glp_subscription(serial_number: str) -> dict:
    """Remove all subscriptions from a device.

    Sends the PATCH and returns immediately once accepted (202).
    """
    from mcp_servers.glp import get_glp_client

    def _do_unassign():
        glp = get_glp_client()
        device_id = glp.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(f"Could not resolve serial {serial_number!r} to a GLP device ID.")
        body = {"subscription": []}
        resp = glp._client._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"GLP PATCH returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "accepted", "http": resp.status_code}

    return await _run(_do_unassign)


def _normalize_mac(mac: str) -> str:
    """Normalize a MAC address to colon-separated uppercase (AA:BB:CC:DD:EE:FF)."""
    import re
    raw = re.sub(r'[^0-9a-fA-F]', '', mac)
    if len(raw) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    upper = raw.upper()
    return ':'.join(upper[i:i+2] for i in range(0, 12, 2))


async def add_glp_device(serial_number: str, mac_address: str) -> dict:
    """Add a single device to the GLP workspace.

    Uses post_async and returns immediately with the task ID.
    """
    from mcp_servers.shared import get_glp_client
    mac = _normalize_mac(mac_address)

    def _do_add():
        glp = get_glp_client()
        body = {"network": [{"serialNumber": serial_number, "macAddress": mac}], "compute": [], "storage": []}
        location = glp._client.post_async("/devices/v1/devices", data=body)
        task_id = location.rstrip("/").split("/")[-1]
        return {"status": "accepted", "task_id": task_id}

    return await _run(_do_add)


async def add_glp_devices_bulk(devices: list[dict]) -> dict:
    """Add multiple devices to the GLP workspace.

    devices: list of {"serialNumber": "...", "macAddress": "..."}
    Returns immediately with the task ID.
    """
    from mcp_servers.shared import get_glp_client
    normalized = []
    for d in devices:
        serial = (d.get("serialNumber") or "").strip()
        mac = (d.get("macAddress") or "").strip()
        if not serial or not mac:
            raise ValueError(
                f"Each device needs serialNumber and macAddress; got: {d!r}"
            )
        normalized.append({"serialNumber": serial, "macAddress": _normalize_mac(mac)})

    def _do_bulk():
        glp = get_glp_client()
        body = {"network": normalized, "compute": [], "storage": []}
        location = glp._client.post_async("/devices/v1/devices", data=body)
        task_id = location.rstrip("/").split("/")[-1]
        return {"status": "accepted", "task_id": task_id, "count": len(normalized)}

    return await _run(_do_bulk)


async def assign_glp_device_to_app(serial_number: str) -> dict:
    """Assign a device to the Aruba Central application in GLP.

    Auto-detects the application ID and region from an existing assigned device.
    Must be called AFTER the device is added to GLP.
    """
    from mcp_servers.shared import get_glp_client

    def _do_assign_app():
        glp = get_glp_client()
        # Find app ID and region from an already-assigned device
        devs = glp.list_devices(limit=50)
        app_id = None
        region = None
        for d in devs:
            if d.get("application") and d.get("region"):
                app_id = d["application"]["id"]
                region = d["region"]
                break
        if not app_id:
            raise RuntimeError("Could not find an existing device with an application assignment to copy from.")

        device_id = glp.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(f"Could not resolve serial {serial_number!r} to a GLP device ID.")

        body = {"application": {"id": app_id}, "region": region}
        resp = glp._client._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"GLP app assign PATCH returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "accepted", "http": resp.status_code, "app_id": app_id, "region": region}

    return await _run(_do_assign_app)


# ── RAG Doc Search ────────────────────────────────────────────────────────────

async def search_docs(query: str, top_k: int = 5) -> list[dict]:
    """Hybrid doc search via centralmcp RAG (LanceDB by default, Redis optional)."""
    try:
        from mcp_servers.rag import search_docs as _search
        return await _run(_search, query, top_k=top_k)
    except Exception as exc:
        logger.warning("Doc search failed: %s", exc)
        return [{"error": str(exc)}]


async def lookup_api(query: str, top_k: int = 10) -> list[dict]:
    """Exact OpenAPI field/endpoint lookup via centralmcp specs index."""
    try:
        from mcp_servers.rag import lookup_api as _lookup
        return await _run(_lookup, query, top_k=top_k)
    except Exception as exc:
        logger.warning("API lookup failed: %s", exc)
        return [{"error": str(exc)}]


async def ask_docs(question: str, top_k: int = 3, source: str | None = None) -> dict:
    """Compact cited answer from local docs/API indexes."""
    try:
        from mcp_servers.rag import ask_docs as _ask
        result = await _run(_ask, question, top_k=top_k, source=source)
        return result if isinstance(result, dict) else {"answer": str(result), "citations": [], "mode": "unknown"}
    except Exception as exc:
        logger.warning("ask_docs failed: %s", exc)
        return {"answer": str(exc), "citations": [], "mode": "error"}


# ── MCP Tool Registry (Lab tester) ────────────────────────────────────────────

TOOL_REGISTRY: dict[str, list[dict]] = {
    "monitoring": [
        {"name": "list_sites",   "desc": "List all sites",                "params": '{"limit": 100}'},
        {"name": "list_devices", "desc": "List APs and switches",         "params": '{"limit": 50}'},
        {"name": "list_clients", "desc": "List connected clients",        "params": '{"limit": 50}'},
        {"name": "find_device",  "desc": "Look up a device by serial",    "params": '{"serial_number": "XXXXX"}'},
        {"name": "find_client",  "desc": "Look up a client by MAC or IP", "params": '{"mac_or_ip": "10.0.0.1"}'},
        {"name": "list_alerts",  "desc": "List active alerts",            "params": '{"limit": 20}'},
        {"name": "list_events",  "desc": "Events for a device (24h)",     "params": '{"serial_number": "XXXXX", "hours": 24}'},
    ],
    "ops": [
        {"name": "cx_ping",       "desc": "Ping from a CX switch",       "params": '{"serial_number": "XXXXX", "destination": "8.8.8.8"}'},
        {"name": "cx_traceroute", "desc": "Traceroute from a CX switch", "params": '{"serial_number": "XXXXX", "destination": "8.8.8.8"}'},
    ],
    "nac": [
        {"name": "list_mac_registrations", "desc": "List NAC MAC registrations", "params": '{"limit": 50}'},
    ],
    "docs": [
        {"name": "search_docs", "desc": "Hybrid search over Aruba docs", "params": '{"query": "how to configure VLAN", "top_k": 5}'},
        {"name": "lookup_api",  "desc": "Exact API schema/enum lookup",  "params": '{"query": "wlan ssid auth-type enum", "top_k": 10}'},
        {"name": "ask_docs",    "desc": "Compact cited doc answer",      "params": '{"question": "How do I assign a device to a site?", "top_k": 3}'},
    ],
}

# Flat map of tool name → (module, function) for dispatch
_TOOL_MAP: dict[str, tuple[str, str]] = {
    "list_sites":              ("mcp_servers.monitoring", "list_sites"),
    "list_devices":            ("mcp_servers.monitoring", "list_devices"),
    "list_clients":            ("mcp_servers.monitoring", "list_clients"),
    "find_device":             ("mcp_servers.monitoring", "find_device"),
    "find_client":             ("mcp_servers.monitoring", "find_client"),
    "list_alerts":             ("mcp_servers.monitoring", "list_alerts"),
    "list_events":             ("mcp_servers.monitoring", "list_events"),
    "cx_ping":                 ("mcp_servers.ops", "cx_ping"),
    "cx_traceroute":           ("mcp_servers.ops", "cx_traceroute"),
    "list_mac_registrations":  ("mcp_servers.nac", "list_mac_registrations"),
    "search_docs":             ("mcp_servers.rag", "search_docs"),
    "lookup_api":              ("mcp_servers.rag", "lookup_api"),
    "ask_docs":                ("mcp_servers.rag", "ask_docs"),
}


async def run_tool(tool_name: str, params_json: str) -> dict:
    """Execute a named centralmcp tool and return a result dict."""
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except json.JSONDecodeError as exc:
        return {
            "tool": tool_name,
            "params": {},
            "output": None,
            "status": "error",
            "error": f"Invalid JSON params: {exc}",
        }

    if tool_name not in _TOOL_MAP:
        output = await invoke_tool_router(tool_name, params)
        if "error" not in output:
            return {"tool": tool_name, "params": params, "output": output, "status": "success", "error": None}
        return {
            "tool": tool_name,
            "params": params,
            "output": None,
            "status": "error",
            "error": output.get("error") or f"Unknown tool: {tool_name}",
        }

    module_path, fn_name = _TOOL_MAP[tool_name]

    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name)
        output = await _run(fn, **params)
        return {"tool": tool_name, "params": params, "output": output, "status": "success", "error": None}
    except Exception as exc:
        logger.warning("Tool %s failed with params %s: %s", tool_name, params, exc)
        return {"tool": tool_name, "params": params, "output": None, "status": "error", "error": str(exc)}
