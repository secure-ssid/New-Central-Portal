"""Platform tools — NAC MAC manager and read-only config/firmware viewer."""
import logging

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
import html

from vendors.aruba_central import aruba

router = APIRouter()
from templates_shared import templates
logger = logging.getLogger(__name__)


@router.get("/nac")
async def nac_manager(request: Request):
    registrations: list[dict] = []
    error = None
    try:
        from vendors.central_bridge import list_mac_registrations
        raw = await list_mac_registrations(limit=200)
        for r in raw:
            if not isinstance(r, dict):
                continue
            registrations.append({
                "mac": r.get("macAddress") or r.get("mac") or "",
                "description": r.get("description") or r.get("name") or "",
                "status": r.get("status") or r.get("registrationStatus") or "",
                "role": r.get("role") or r.get("userRole") or "",
            })
    except Exception as exc:
        logger.warning("NAC registrations unavailable: %s", exc)
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "platform/nac.html",
        {"registrations": registrations, "error": error, "active": "nac"},
    )


@router.get("/config")
async def config_viewer(request: Request):
    devices = await aruba.get_devices()
    compliance = None
    compliance_error = None
    try:
        from vendors.central_bridge import get_firmware_compliance
        compliance = await get_firmware_compliance(limit=200)
    except Exception as exc:
        logger.warning("Firmware compliance unavailable: %s", exc)
        compliance_error = str(exc)

    return templates.TemplateResponse(
        request,
        "platform/config.html",
        {
            "devices": devices[:100],
            "compliance": compliance,
            "compliance_error": compliance_error,
            "active": "config",
        },
    )


@router.post("/config/running")
async def running_config(request: Request, serial: str = Form(...)):
    from vendors.central_bridge import get_device_running_config

    serial = (serial or "").strip()
    device = await aruba.get_device(serial)
    if not device:
        return HTMLResponse("<p style='color:#f87171;'>Device not found.</p>")
    try:
        result = await get_device_running_config(serial)
        text = ""
        if isinstance(result, dict):
            text = result.get("config") or result.get("output") or str(result)
        else:
            text = str(result)
        return HTMLResponse(
            f"<pre style='font-size:.72rem;color:#94a3b8;white-space:pre-wrap;word-break:break-all;'>"
            f"{html.escape(str(text))}</pre>"
        )
    except Exception as exc:
        logger.exception("Running config fetch failed for %s", serial)
        return HTMLResponse(f"<p style='color:#f87171;'>{html.escape(str(exc))}</p>")
