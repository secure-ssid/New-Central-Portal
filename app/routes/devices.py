from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from vendors.aruba_central import aruba
import asyncio
import html
import ipaddress
import json
import logging
import re

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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
    return templates.TemplateResponse(
        request,
        "devices/list.html",
        {"devices": devices, "groups": groups, "sites": sites, "active": "devices"},
    )


@router.get("/{serial}")
async def device_detail(request: Request, serial: str):
    """Rich detail view for a single device - the drill-down target."""
    from vendors.central_bridge import get_switch_ports, get_device_events

    device = await aruba.get_device(serial)
    if not device:
        raise HTTPException(404, "Device not found")

    # Fetch clients, ports, and events in parallel
    all_clients_task = aruba.get_clients()
    events_task = get_device_events(serial, hours=48, limit=20)

    ports_error = False
    if device.get("type") == "switch":
        ports_task = get_switch_ports(serial)
        all_clients, events, raw_ports = await asyncio.gather(
            all_clients_task, events_task, ports_task, return_exceptions=True
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
    else:
        all_clients, events = await asyncio.gather(
            all_clients_task, events_task, return_exceptions=True
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

    device_name = device.get("name", "")
    connected_clients = [
        c for c in all_clients if c.get("connected_to") == device_name
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
