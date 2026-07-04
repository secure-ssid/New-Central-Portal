from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pagination import paginate as _paginate
from vendors.aruba_central import aruba
import asyncio
import html
import ipaddress
import json
import logging
import re

from templates_shared import templates

logger = logging.getLogger(__name__)

router = APIRouter()

def _wireless_cards(
    wireless_metrics: dict | None,
    ap_radios: dict | None,
    channel_util: dict | None,
) -> dict:
    """Normalize AP wireless payloads into template-friendly cards."""
    radios: list[dict] = []
    if isinstance(ap_radios, dict):
        items = ap_radios.get("radios") or ap_radios.get("items") or ap_radios.get("data") or []
        if isinstance(items, list):
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                radios.append({
                    "band": str(raw.get("band") or raw.get("radioBand") or raw.get("wirelessBand") or "—"),
                    "channel": str(raw.get("channel") or raw.get("wirelessChannel") or "—"),
                    "power": str(raw.get("txPower") or raw.get("power") or raw.get("eirp") or "—"),
                    "clients": str(raw.get("numClients") or raw.get("clientCount") or raw.get("clients") or "—"),
                    "util": str(raw.get("utilization") or raw.get("channelUtilization") or "—"),
                })

    metrics: list[dict] = []
    if isinstance(wireless_metrics, dict):
        for key in (
            "noiseFloor", "clientCount", "cpu", "memory", "txBytes", "rxBytes",
            "txRate", "rxRate", "uptime", "status",
        ):
            val = wireless_metrics.get(key)
            if val not in (None, ""):
                label = key.replace("_", " ")
                metrics.append({"label": label, "value": str(val)})

    util_pct = None
    if isinstance(channel_util, dict):
        util_pct = (
            channel_util.get("utilization")
            or channel_util.get("channelUtilization")
            or channel_util.get("avgUtilization")
        )
        if util_pct is not None:
            util_pct = str(util_pct)

    return {"radios": radios, "metrics": metrics[:8], "channel_util_pct": util_pct}


def _normalize_ports(raw_ports) -> list[dict]:
    """Normalize raw Central switch-port dicts into a stable contract.

    Guaranteed fields per port:
      name (str), index (int), alignment ('top'|'bottom'),
      connected (bool), uplink (bool), poe (bool),
      neighbour (str|None), speed_mbps (int|None).
    """
    normalized: list[dict] = []
    if not isinstance(raw_ports, list):
        return normalized

    cleaned = []
    any_alignment = False
    for i, p in enumerate(raw_ports):
        if not isinstance(p, dict):
            continue

        # index — fall back to list position
        try:
            idx = int(p.get("index"))
        except (TypeError, ValueError):
            idx = i + 1

        # alignment — tolerate missing/None/odd casing
        raw_align = p.get("portAlignment") or p.get("port_alignment") or ""
        align = str(raw_align).strip().lower()
        if align not in ("top", "bottom"):
            align = None
        else:
            any_alignment = True

        # connected — from status string
        status = str(p.get("status") or "").strip().lower()
        connected = status in ("connected", "up")

        # poe — anything other than absent / "Not Used"
        poe_status = str(p.get("poeStatus") or p.get("poe_status") or "").strip()
        poe = bool(poe_status) and poe_status.lower() != "not used"

        # neighbour
        neighbour = p.get("neighbour") or p.get("neighbor") or None
        if neighbour is not None:
            neighbour = str(neighbour).strip() or None

        # speed — raw value is bps; guard against None/0/strings
        speed_mbps = None
        try:
            bps = float(p.get("speed"))
            if bps > 0:
                speed_mbps = int(bps / 1_000_000)
        except (TypeError, ValueError):
            pass

        name = str(p.get("name") or p.get("id") or f"Port {idx}").strip()

        cleaned.append({
            "name": name,
            "index": idx,
            "alignment": align,
            "connected": connected,
            "uplink": bool(p.get("uplink")),
            "poe": poe,
            "neighbour": neighbour,
            "speed_mbps": speed_mbps,
        })

    for port in cleaned:
        if port["alignment"] is None:
            if any_alignment:
                # Some ports carry alignment: slot the rest in by parity.
                port["alignment"] = "top" if port["index"] % 2 == 1 else "bottom"
            else:
                # No alignment data at all: render a single row.
                port["alignment"] = "top"

    cleaned.sort(key=lambda x: x["index"])
    return cleaned


@router.get("/")
async def list_devices(request: Request):
    """Full devices list page."""
    from vendors.central_bridge import get_device_groups, get_central_sites
    devices, groups, sites = await asyncio.gather(
        aruba.get_devices(),
        get_device_groups(),
        get_central_sites(),
        return_exceptions=True,
    )
    if isinstance(devices, Exception): devices = []
    if isinstance(groups, Exception): groups = []
    if isinstance(sites, Exception): sites = []
    pg = _paginate(request, devices)  # slice after fetch/filter logic
    return templates.TemplateResponse(
        request,
        "devices/list.html",
        {
            "devices": pg["items"],
            "groups": groups,
            "sites": sites,
            "active": "devices",
            "page": pg["page"],
            "per_page": pg["per_page"],
            "total": pg["total"],
            "total_pages": pg["total_pages"],
            "has_prev": pg["has_prev"],
            "has_next": pg["has_next"],
            "base_qs": pg["base_qs"],
        },
    )


@router.get("/{serial}")
async def device_detail(request: Request, serial: str):
    """Rich detail view for a single device - the drill-down target."""
    from vendors.central_bridge import get_device_events, get_device_health, get_switch_ports

    device = await aruba.get_device(serial)
    if not device:
        raise HTTPException(404, "Device not found")

    health_task = get_device_health(serial)
    clients_task = aruba.get_clients(limit=200)
    events_task = get_device_events(serial, hours=48, limit=20)
    health_label = None
    wireless_metrics = None
    ap_radios = None
    channel_util = None

    ports_error = False
    if device.get("type") == "switch":
        ports_task = get_switch_ports(serial)
        all_clients, events, raw_ports, health = await asyncio.gather(
            clients_task, events_task, ports_task, health_task, return_exceptions=True
        )
        if isinstance(raw_ports, Exception):
            logger.error("Failed to fetch switch ports for %s: %s", serial, raw_ports)
            ports_error = True
            raw_ports = []
        elif not isinstance(raw_ports, list):
            logger.warning(
                "Unexpected switch-port payload for %s: %r", serial, type(raw_ports)
            )
            ports_error = True
            raw_ports = []
    elif device.get("type") == "access_point":
        try:
            from vendors.central_bridge import get_ap_radios, get_channel_utilization, get_wireless_metrics
            all_clients, events, health, wireless_metrics, ap_radios, channel_util = await asyncio.gather(
                clients_task, events_task, health_task,
                get_wireless_metrics(serial), get_ap_radios(serial), get_channel_utilization(serial),
                return_exceptions=True,
            )
        except Exception:
            all_clients, events, health = await asyncio.gather(
                clients_task, events_task, health_task, return_exceptions=True
            )
            wireless_metrics = ap_radios = channel_util = None
        raw_ports = []
    else:
        all_clients, events, health = await asyncio.gather(
            clients_task, events_task, health_task, return_exceptions=True
        )
        raw_ports = []

    try:
        ports = _normalize_ports(raw_ports)
    except Exception:
        logger.exception("Failed to normalize switch ports for %s", serial)
        ports = []
        ports_error = True

    if isinstance(all_clients, Exception):
        all_clients = []
    if isinstance(events, Exception):
        events = []
    if isinstance(health, Exception):
        health = None
    elif isinstance(health, dict):
        from routes.home import _health_issue_label
        health_label = _health_issue_label(health)
    if isinstance(wireless_metrics, Exception):
        wireless_metrics = None
    if isinstance(ap_radios, Exception):
        ap_radios = None
    if isinstance(channel_util, Exception):
        channel_util = None

    device_name = device.get("name", "")
    connected_clients = [
        c for c in all_clients
        if c.get("connected_device_serial") == serial or c.get("connected_to") == device_name
    ]

    # Serialized for the template's JS (3D faceplate). Escape "</" so the
    # payload can never close its enclosing <script> tag.
    ports_json = json.dumps(ports).replace("</", "<\\/")

    return templates.TemplateResponse(
        request,
        "devices/detail.html",
        {
            "device": device,
            "clients": connected_clients,
            "ports": ports,
            "ports_error": ports_error,
            "ports_json": ports_json,
            "events": events,
            "health_label": health_label,
            "wireless_metrics": wireless_metrics if isinstance(wireless_metrics, dict) else None,
            "ap_radios": ap_radios if isinstance(ap_radios, dict) else None,
            "channel_util": channel_util if isinstance(channel_util, dict) else None,
            "wireless_cards": _wireless_cards(
                wireless_metrics if isinstance(wireless_metrics, dict) else None,
                ap_radios if isinstance(ap_radios, dict) else None,
                channel_util if isinstance(channel_util, dict) else None,
            ),
            "active": "devices",
        },
    )


# ── Ops: input validation helpers ─────────────────────────────────────────────

MAX_SHOW_COMMANDS = 5
MAX_SHOW_COMMAND_LEN = 120

# Conservative character allowlist for show commands: letters, digits, spaces
# and a few interface/VRF punctuation chars. No quotes, pipes, backticks,
# semicolons, redirects, newlines, etc.
_SHOW_CMD_SAFE_RE = re.compile(r"^[A-Za-z0-9 _/.,:*+-]+$")

# RFC-1123-ish hostname: dot-separated labels of alnum + inner hyphens.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def _ops_error(message: str) -> HTMLResponse:
    """Friendly error for the #ops-output HTMX target.

    Served with 200 because htmx does not swap 4xx response bodies into the
    target by default — the red styling is the user-facing error signal.
    """
    return HTMLResponse(f"<p style='color:#f87171;'>{html.escape(message)}</p>")


def _parse_show_commands(raw: str) -> tuple[list[str] | None, str | None]:
    """Validate a ';'-separated command string. Returns (commands, error)."""
    cmds = [" ".join(c.split()) for c in (raw or "").split(";")]
    cmds = [c for c in cmds if c]
    if not cmds:
        return None, "No command provided."
    if len(cmds) > MAX_SHOW_COMMANDS:
        return None, f"Too many commands — max {MAX_SHOW_COMMANDS} per request."
    for c in cmds:
        if len(c) > MAX_SHOW_COMMAND_LEN:
            return None, f"Command too long — max {MAX_SHOW_COMMAND_LEN} characters."
        if not _SHOW_CMD_SAFE_RE.fullmatch(c):
            return None, "Command contains unsupported characters."
        if not c.lower().startswith("show "):
            return None, 'Only "show ..." commands are allowed.'
    return cmds, None


def _validate_ping_destination(destination: str) -> str | None:
    """Return a cleaned destination if it is a valid IP or hostname, else None."""
    dest = (destination or "").strip()
    if not dest or len(dest) > 253:
        return None
    if "%" in dest:  # reject scoped IPv6 zone-ids (fe80::1%eth0) — keep it simple
        return None
    try:
        ipaddress.ip_address(dest)
        return dest
    except ValueError:
        pass
    if _HOSTNAME_RE.fullmatch(dest):
        return dest
    return None


# ── Ops: show command ──────────────────────────────────────────────────────────

@router.post("/{serial}/show")
async def device_show(request: Request, serial: str, command: str = Form("show version")):
    from vendors.central_bridge import run_show
    cmds, err = _parse_show_commands(command)
    if err:
        return _ops_error(err)
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    try:
        result = await run_show(serial, device.get("type", "switch"), cmds)
        outputs = result.get("output", {}).get("results", []) if isinstance(result, dict) else []
        html_parts = []
        for item in outputs:
            if not isinstance(item, dict):
                continue
            cmd_label = html.escape(str(item.get("command", "")))
            out_text = html.escape(str(item.get("output", "")))
            html_parts.append(
                f'<p style="font-size:.65rem;color:#f97316;font-weight:700;margin-bottom:4px;">{cmd_label}</p>'
                f'<pre style="font-size:.72rem;color:#94a3b8;white-space:pre-wrap;word-break:break-all;margin-bottom:14px;">{out_text}</pre>'
            )
        return HTMLResponse("".join(html_parts) or "<p style='color:#6b7280;'>No output.</p>")
    except Exception as e:
        logger.exception("show command failed for %s", serial)
        return _ops_error(f"Error: {e}")


# ── Ops: ping ─────────────────────────────────────────────────────────────────

@router.post("/{serial}/ping")
async def device_ping(
    request: Request,
    serial: str,
    destination: str = Form(...),
    count: int = Form(5),
):
    from vendors.central_bridge import run_ping
    dest = _validate_ping_destination(destination)
    if dest is None:
        return _ops_error("Invalid destination — enter an IPv4/IPv6 address or a hostname.")
    count = max(1, min(10, count))
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    try:
        result = await run_ping(serial, device.get("type", "switch"), dest, count=count)
        if not isinstance(result, dict):
            result = {}
        status = html.escape(str(result.get("status", "")))
        outputs = result.get("output", {}).get("results", [])
        raw_text = outputs[0].get("output", "") if outputs and isinstance(outputs[0], dict) else str(result)
        color = "#4ade80" if "success" in str(raw_text).lower() else "#f87171"
        text = html.escape(str(raw_text))
        return HTMLResponse(
            f'<p style="font-size:.72rem;color:{color};font-weight:700;margin-bottom:6px;">Status: {status}</p>'
            f'<pre style="font-size:.72rem;color:#94a3b8;white-space:pre-wrap;">{text}</pre>'
        )
    except Exception as e:
        logger.exception("ping failed for %s", serial)
        return _ops_error(f"Error: {e}")


def _validate_mac(mac: str) -> str | None:
    """Return normalized MAC if valid, else None."""
    raw = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(raw) != 12:
        return None
    upper = raw.upper()
    return ":".join(upper[i:i + 2] for i in range(0, 12, 2))


def _long_running_notice() -> str:
    return (
        '<p style="font-size:.72rem;color:#94a3b8;margin-bottom:8px;">'
        '<span class="spinner" style="vertical-align:middle;margin-right:6px;"></span>'
        "Running — may take up to 60s…</p>"
    )


# ── Ops: traceroute ───────────────────────────────────────────────────────────

@router.post("/{serial}/traceroute")
async def device_traceroute(request: Request, serial: str, destination: str = Form(...)):
    from vendors.central_bridge import run_traceroute
    dest = _validate_ping_destination(destination)
    if dest is None:
        return _ops_error("Invalid destination — enter an IPv4/IPv6 address or a hostname.")
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    try:
        result = await run_traceroute(serial, device.get("type", "switch"), dest)
        if not isinstance(result, dict):
            result = {}
        outputs = result.get("output", {}).get("results", []) if isinstance(result.get("output"), dict) else []
        if result.get("errors"):
            return HTMLResponse(
                _long_running_notice()
                + f"<p style='color:#f87171;'>{html.escape('; '.join(result['errors']))}</p>"
            )
        html_parts = []
        for item in outputs:
            if not isinstance(item, dict):
                continue
            out_text = html.escape(str(item.get("output", "")))
            html_parts.append(f'<pre style="font-size:.72rem;color:#94a3b8;white-space:pre-wrap;">{out_text}</pre>')
        body = "".join(html_parts) or f"<pre style='font-size:.72rem;color:#94a3b8;'>{html.escape(str(result))}</pre>"
        return HTMLResponse(body)
    except Exception as e:
        logger.exception("traceroute failed for %s", serial)
        return _ops_error(f"Error: {e}")


# ── Ops: LLDP neighbors ───────────────────────────────────────────────────────

@router.post("/{serial}/lldp")
async def device_lldp(request: Request, serial: str):
    from vendors.central_bridge import get_lldp_neighbors
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    try:
        result = await get_lldp_neighbors(serial)
        if isinstance(result, dict):
            neighbors = result.get("neighbors") or result.get("items") or result.get("lldp") or []
            if isinstance(neighbors, list) and neighbors:
                rows = []
                for n in neighbors:
                    if not isinstance(n, dict):
                        continue
                    rows.append(
                        f"<tr><td>{html.escape(str(n.get('localPort') or n.get('port') or ''))}</td>"
                        f"<td>{html.escape(str(n.get('neighborName') or n.get('systemName') or ''))}</td>"
                        f"<td>{html.escape(str(n.get('neighborPort') or n.get('portId') or ''))}</td></tr>"
                    )
                if rows:
                    return HTMLResponse(
                        "<table class='tbl'><thead><tr><th>Local</th><th>Neighbor</th><th>Remote Port</th></tr></thead>"
                        f"<tbody>{''.join(rows)}</tbody></table>"
                    )
            text = result.get("output") or result.get("raw") or str(result)
            return HTMLResponse(f"<pre style='font-size:.72rem;color:#94a3b8;white-space:pre-wrap;'>{html.escape(str(text))}</pre>")
        return HTMLResponse(f"<pre style='font-size:.72rem;color:#94a3b8;'>{html.escape(str(result))}</pre>")
    except Exception as e:
        logger.exception("LLDP failed for %s", serial)
        return _ops_error(f"Error: {e}")


# ── Ops: port errors ────────────────────────────────────────────────────────────

@router.post("/{serial}/port-errors")
async def device_port_errors(request: Request, serial: str, interface: str = Form("")):
    from vendors.central_bridge import get_switch_port_errors
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    iface = (interface or "").strip() or None
    try:
        result = await get_switch_port_errors(serial, interface=iface)
        if isinstance(result, dict):
            ports = result.get("ports") or result.get("interfaces") or result.get("items") or []
            if isinstance(ports, list) and ports:
                rows = []
                for p in ports:
                    if not isinstance(p, dict):
                        continue
                    sev = str(p.get("severity") or p.get("status") or "").lower()
                    color = "#f87171" if "error" in sev or "critical" in sev else "#94a3b8"
                    rows.append(
                        f"<tr><td>{html.escape(str(p.get('interface') or p.get('name') or ''))}</td>"
                        f"<td style='color:{color};'>{html.escape(str(p.get('errors') or p.get('errorCount') or p.get('count') or ''))}</td>"
                        f"<td>{html.escape(str(p.get('severity') or ''))}</td></tr>"
                    )
                if rows:
                    return HTMLResponse(
                        "<table class='tbl'><thead><tr><th>Interface</th><th>Errors</th><th>Severity</th></tr></thead>"
                        f"<tbody>{''.join(rows)}</tbody></table>"
                    )
        return HTMLResponse(f"<pre style='font-size:.72rem;color:#94a3b8;'>{html.escape(str(result))}</pre>")
    except Exception as e:
        logger.exception("Port errors failed for %s", serial)
        return _ops_error(f"Error: {e}")


# ── Ops: find MAC ───────────────────────────────────────────────────────────────

@router.post("/{serial}/find-mac")
async def device_find_mac(request: Request, serial: str, mac_address: str = Form(...)):
    from vendors.central_bridge import find_mac_on_switch
    mac = _validate_mac(mac_address)
    if not mac:
        return _ops_error("Invalid MAC address.")
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    try:
        result = await find_mac_on_switch(serial, mac)
        if isinstance(result, dict):
            port = result.get("port") or result.get("interface") or result.get("vlan")
            if port:
                return HTMLResponse(
                    f"<p style='color:#4ade80;font-size:.8rem;'>MAC <strong>{html.escape(mac)}</strong> "
                    f"found on port <strong>{html.escape(str(port))}</strong></p>"
                )
        return HTMLResponse(f"<pre style='font-size:.72rem;color:#94a3b8;'>{html.escape(str(result))}</pre>")
    except Exception as e:
        logger.exception("find-mac failed for %s", serial)
        return _ops_error(f"Error: {e}")


@router.post("/{serial}/mac-table")
async def device_mac_table(request: Request, serial: str, interface: str = Form("")):
    from vendors.central_bridge import get_cx_mac_table
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    if device.get("type") != "switch":
        return _ops_error("MAC table is only available on switches.")
    iface = (interface or "").strip() or None
    try:
        result = await get_cx_mac_table(serial, interface=iface)
        entries = []
        if isinstance(result, dict):
            entries = result.get("entries") or result.get("macs") or result.get("items") or []
            if not entries and isinstance(result.get("output"), dict):
                entries = result["output"].get("results", [])
        elif isinstance(result, list):
            entries = result
        if entries:
            rows = []
            for e in entries[:100]:
                if not isinstance(e, dict):
                    continue
                rows.append(
                    f"<tr><td class='font-mono text-xs'>{html.escape(str(e.get('mac') or e.get('macAddress') or ''))}</td>"
                    f"<td>{html.escape(str(e.get('vlan') or e.get('vlanId') or ''))}</td>"
                    f"<td>{html.escape(str(e.get('port') or e.get('interface') or e.get('name') or ''))}</td>"
                    f"<td>{html.escape(str(e.get('type') or e.get('entryType') or ''))}</td></tr>"
                )
            if rows:
                note = f"<p class='text-[11px] text-slate-600 mb-2'>Showing {len(rows)} entr{'y' if len(rows)==1 else 'ies'}</p>"
                return HTMLResponse(
                    note
                    + "<table class='tbl'><thead><tr><th>MAC</th><th>VLAN</th><th>Port</th><th>Type</th></tr></thead>"
                    f"<tbody>{''.join(rows)}</tbody></table>"
                )
        return HTMLResponse(f"<pre style='font-size:.72rem;color:#94a3b8;'>{html.escape(str(result))}</pre>")
    except Exception as e:
        logger.exception("mac-table failed for %s", serial)
        return _ops_error(f"Error: {e}")


# ── Device Management: group & site assignment ──────────────────────────────

@router.post("/assign-group")
async def assign_group(request: Request):
    body = await request.json()
    group_name = body.get("group_name", "").strip()
    serials = body.get("serial_numbers", [])
    if not group_name or not serials:
        return JSONResponse({"ok": False, "error": "group_name and serial_numbers are required"}, status_code=400)
    try:
        from vendors.central_bridge import move_device_to_group
        result = await move_device_to_group(group_name, serials)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/assign-site")
async def assign_site(request: Request):
    body = await request.json()
    serials = body.get("serial_numbers", [])
    site_id = body.get("site_id")
    device_type = body.get("device_type", "").strip() or "IAP"
    if not serials or site_id is None:
        return JSONResponse({"ok": False, "error": "serial_numbers and site_id are required"}, status_code=400)
    try:
        from vendors.central_bridge import assign_device_to_site
        result = await assign_device_to_site(int(site_id), serials, device_type)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@router.post("/{serial}/reboot")
async def device_reboot(request: Request, serial: str):
    from vendors.central_bridge import run_reboot
    device = await aruba.get_device(serial)
    if not device:
        return _ops_error("Device not found.")
    try:
        result = await run_reboot(serial, device.get("type", "switch"))
        status = html.escape(str(result.get("status", "submitted") if isinstance(result, dict) else "submitted"))
        return HTMLResponse(f"<p style='color:#4ade80;'>Reboot {status}. Device will be offline for ~60s.</p>")
    except Exception as e:
        logger.exception("reboot failed for %s", serial)
        return _ops_error(f"Error: {e}")
