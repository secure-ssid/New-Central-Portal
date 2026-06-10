"""Bridge to centralmcp tools — async wrappers for FastAPI.

Requires these env vars (set in docker-compose.yml):
  PYTHONPATH=/centralmcp
  CREDS_PATH=/centralmcp/config/credentials.yaml
  QDRANT_URL=http://host.docker.internal:6333   (for RAG)
  OLLAMA_URL=http://host.docker.internal:11434  (for RAG)
  CLASSIC_CENTRAL_BASE_URL, CLASSIC_CENTRAL_CLIENT_ID, etc. (Classic Central)
"""
import asyncio
import importlib
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
    """Run a blocking centralmcp function in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


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
) -> list[dict]:
    from mcp_servers.monitoring import list_devices
    kwargs: dict[str, Any] = {"limit": limit}
    if device_type:
        kwargs["device_type"] = device_type
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_devices, **kwargs))


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
    from mcp_servers.monitoring import list_devices
    all_devices = _unwrap(await _run(list_devices, limit=50))
    switches = [
        d for d in all_devices
        if isinstance(d, dict)
        and (d.get("deviceType") or "").upper() in ("SWITCH", "AOS_S", "AOS-S", "CX", "AOS_CX")
    ]
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


# ── Clients ──────────────────────────────────────────────────────────────────

async def get_clients(
    site_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    from mcp_servers.monitoring import list_clients
    kwargs: dict[str, Any] = {"limit": limit}
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_clients, **kwargs))


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
    dtype = device_type.lower()
    if "switch" in dtype or dtype in ("cx", "aos_cx"):
        from mcp_servers.ops import cx_show
        return await _run(cx_show, serial, commands)
    elif "access_point" in dtype or dtype == "ap":
        from mcp_servers.ops import aos_s_show
        return await _run(aos_s_show, serial, commands)
    elif "gateway" in dtype or dtype == "gw":
        from mcp_servers.ops import gateway_show
        return await _run(gateway_show, serial, commands)
    else:
        from mcp_servers.ops import cx_show
        return await _run(cx_show, serial, commands)


async def run_ping(serial: str, device_type: str, destination: str, count: int = 5) -> dict:
    dtype = device_type.lower()
    if "switch" in dtype or dtype in ("cx", "aos_cx"):
        from mcp_servers.ops import cx_ping
        return await _run(cx_ping, serial, destination, count)
    else:
        from mcp_servers.ops import aos_s_ping
        return await _run(aos_s_ping, serial, destination)


async def run_reboot(serial: str, device_type: str) -> dict:
    from mcp_servers.ops import reboot_device
    return await _run(reboot_device, serial_number=serial, device_type=device_type or None)


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

def search_docs(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over Aruba docs via Qdrant + Ollama.

    Uses QDRANT_URL and OLLAMA_URL env vars so the URLs are configurable
    inside Docker (avoids the hardcoded localhost in centralmcp's rag.py).
    """
    qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")

    try:
        from qdrant_client import QdrantClient
        from pipeline.clients.qdrant_client import DOCS_COLLECTION
        from pipeline.clients.ollama_client import OllamaClient

        ollama = OllamaClient(url=ollama_url)
        qdrant = QdrantClient(url=qdrant_url)

        query_vector = ollama.embed(query)
        hits = qdrant.query_points(
            collection_name=DOCS_COLLECTION,
            query=query_vector,
            limit=min(top_k, 20),
        )

        return [
            {
                "text": h.payload.get("text", ""),
                "source": h.payload.get("source", ""),
                "file_path": h.payload.get("file_path", ""),
                "score": h.score,
            }
            for h in hits.points
        ]
    except Exception as exc:
        logger.warning("Doc search failed (qdrant=%s, ollama=%s): %s", qdrant_url, ollama_url, exc)
        return [{"error": str(exc)}]


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
        {"name": "search_docs", "desc": "Semantic search over Aruba docs", "params": '{"query": "how to configure VLAN", "top_k": 5}'},
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
        return {
            "tool": tool_name,
            "params": params,
            "output": None,
            "status": "error",
            "error": f"Unknown tool: {tool_name}",
        }

    module_path, fn_name = _TOOL_MAP[tool_name]

    # search_docs goes through the bridge implementation to use env-based URLs
    if tool_name == "search_docs":
        output = search_docs(**params)
        return {"tool": tool_name, "params": params, "output": output, "status": "success", "error": None}

    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name)
        output = await _run(fn, **params)
        return {"tool": tool_name, "params": params, "output": output, "status": "success", "error": None}
    except Exception as exc:
        logger.warning("Tool %s failed with params %s: %s", tool_name, params, exc)
        return {"tool": tool_name, "params": params, "output": None, "status": "error", "error": str(exc)}
