"""Platform tools — NAC MAC manager and read-only config/firmware viewer."""
import logging

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
import html

from bridge_errors import BRIDGE_UNAVAILABLE
from pagination import filter_items, paginate as _paginate
from vendors.aruba_central import aruba

from templates_shared import templates

router = APIRouter()
logger = logging.getLogger(__name__)

_COMPLIANT_STATUSES = frozenset({
    "compliant", "ok", "up to date", "uptodate", "current", "yes", "true", "up-to-date",
})


def _is_compliant_status(status: str) -> bool:
    return (status or "").strip().lower() in _COMPLIANT_STATUSES


def _normalize_firmware_compliance(raw) -> dict:
    """Turn centralmcp firmware compliance payloads into summary + table rows."""
    items: list = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("items", "devices", "data", "compliance", "results", "records"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                items = candidate
                break

    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = (
            item.get("complianceStatus")
            or item.get("compliance")
            or item.get("status")
            or ""
        )
        rows.append({
            "serial": (
                item.get("serialNumber") or item.get("serial") or item.get("deviceSerial") or ""
            ),
            "name": item.get("deviceName") or item.get("name") or "",
            "model": item.get("model") or item.get("deviceModel") or "",
            "current": (
                item.get("firmwareVersion")
                or item.get("currentVersion")
                or item.get("installedVersion")
                or item.get("version")
                or ""
            ),
            "target": (
                item.get("targetVersion")
                or item.get("recommendedVersion")
                or item.get("assignedVersion")
                or item.get("requiredVersion")
                or ""
            ),
            "status": str(status),
            "site": item.get("siteName") or item.get("site") or "",
        })

    compliant = sum(1 for r in rows if _is_compliant_status(r["status"]))
    return {
        "summary": {
            "total": len(rows),
            "compliant": compliant,
            "non_compliant": max(0, len(rows) - compliant),
        },
        "rows": rows,
    }


@router.get("/nac")
async def nac_manager(request: Request):
    registrations: list[dict] = []
    error = None
    q = request.query_params.get("q", "").strip()
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
        error = BRIDGE_UNAVAILABLE

    registrations = filter_items(registrations, q, "mac", "description", "status", "role")
    pg = _paginate(request, registrations)

    return templates.TemplateResponse(
        request,
        "platform/nac.html",
        {
            "registrations": pg["items"],
            "error": error,
            "q": q,
            "active": "nac",
            "page": pg["page"],
            "per_page": pg["per_page"],
            "total": pg["total"],
            "total_pages": pg["total_pages"],
            "has_prev": pg["has_prev"],
            "has_next": pg["has_next"],
            "base_qs": pg["base_qs"],
        },
    )


@router.get("/config")
async def config_viewer(request: Request):
    devices = await aruba.get_devices()
    compliance = None
    compliance_error = None
    try:
        from vendors.central_bridge import get_firmware_compliance
        raw = await get_firmware_compliance(limit=200)
        compliance = _normalize_firmware_compliance(raw)
    except Exception as exc:
        logger.warning("Firmware compliance unavailable: %s", exc)
        compliance_error = BRIDGE_UNAVAILABLE

    compliance_preview_limit = 50
    compliance_rows = compliance.get("rows", []) if compliance else []
    compliance_total = len(compliance_rows)

    return templates.TemplateResponse(
        request,
        "platform/config.html",
        {
            "devices": devices[:100],
            "compliance": compliance,
            "compliance_rows": compliance_rows[:compliance_preview_limit],
            "compliance_total": compliance_total,
            "compliance_preview_limit": compliance_preview_limit,
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
    except Exception:
        logger.exception("Running config fetch failed for %s", serial)
        return HTMLResponse(
            f"<p style='color:#f87171;'>{html.escape(BRIDGE_UNAVAILABLE)}</p>"
        )
