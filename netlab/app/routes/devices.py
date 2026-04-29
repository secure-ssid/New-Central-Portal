from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from vendors.aruba_central import aruba
import asyncio

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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

    if device.get("type") == "switch":
        ports_task = get_switch_ports(serial)
        all_clients, events, ports = await asyncio.gather(
            all_clients_task, events_task, ports_task, return_exceptions=True
        )
        ports = ports if isinstance(ports, list) else []
    else:
        all_clients, events = await asyncio.gather(
            all_clients_task, events_task, return_exceptions=True
        )
        ports = []

    if isinstance(all_clients, Exception):
        all_clients = []
    if isinstance(events, Exception):
        events = []

    device_name = device.get("name", "")
    connected_clients = [
        c for c in all_clients if c.get("connected_to") == device_name
    ]

    return templates.TemplateResponse(
        request,
        "devices/detail.html",
        {
            "device": device,
            "clients": connected_clients,
            "ports": ports,
            "events": events,
            "active": "devices",
        },
    )


# ── Ops: show command ──────────────────────────────────────────────────────────

@router.post("/{serial}/show")
async def device_show(request: Request, serial: str, command: str = Form("show version")):
    from vendors.central_bridge import run_show
    device = await aruba.get_device(serial)
    if not device:
        return HTMLResponse("<p style='color:#f87171;'>Device not found.</p>")
    cmds = [c.strip() for c in command.split(";") if c.strip()]
    try:
        result = await run_show(serial, device.get("type", "switch"), cmds)
        outputs = result.get("output", {}).get("results", [])
        html_parts = []
        for item in outputs:
            html_parts.append(
                f'<p style="font-size:.65rem;color:#f97316;font-weight:700;margin-bottom:4px;">{item["command"]}</p>'
                f'<pre style="font-size:.72rem;color:#94a3b8;white-space:pre-wrap;word-break:break-all;margin-bottom:14px;">{item.get("output","")}</pre>'
            )
        return HTMLResponse("".join(html_parts) or "<p style='color:#6b7280;'>No output.</p>")
    except Exception as e:
        return HTMLResponse(f"<p style='color:#f87171;'>Error: {e}</p>")


# ── Ops: ping ─────────────────────────────────────────────────────────────────

@router.post("/{serial}/ping")
async def device_ping(request: Request, serial: str, destination: str = Form(...)):
    from vendors.central_bridge import run_ping
    device = await aruba.get_device(serial)
    if not device:
        return HTMLResponse("<p style='color:#f87171;'>Device not found.</p>")
    try:
        result = await run_ping(serial, device.get("type", "switch"), destination, count=5)
        status = result.get("status", "")
        outputs = result.get("output", {}).get("results", [])
        text = outputs[0].get("output", "") if outputs else str(result)
        color = "#4ade80" if "success" in text.lower() else "#f87171"
        return HTMLResponse(
            f'<p style="font-size:.72rem;color:{color};font-weight:700;margin-bottom:6px;">Status: {status}</p>'
            f'<pre style="font-size:.72rem;color:#94a3b8;white-space:pre-wrap;">{text}</pre>'
        )
    except Exception as e:
        return HTMLResponse(f"<p style='color:#f87171;'>Error: {e}</p>")


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
        return HTMLResponse("<p style='color:#f87171;'>Device not found.</p>")
    try:
        result = await run_reboot(serial, device.get("type", "switch"))
        status = result.get("status", "submitted")
        return HTMLResponse(f"<p style='color:#4ade80;'>Reboot {status}. Device will be offline for ~60s.</p>")
    except Exception as e:
        return HTMLResponse(f"<p style='color:#f87171;'>Error: {e}</p>")
