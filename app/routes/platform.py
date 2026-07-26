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
    # "unknown" (no running or no recommended version) is not drift — counting
    # it as "need attention" was what made this page claim 11 when the real
    # drift is a handful.
    unknown = sum(1 for r in rows
                  if not _is_compliant_status(r["status"])
                  and str(r["status"]).lower() in ("unknown", ""))
    return {
        "summary": {
            "total": len(rows),
            "compliant": compliant,
            "unknown": unknown,
            "non_compliant": max(0, len(rows) - compliant - unknown),
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
            # The MAC-registration payload has none of description/status/role.
            # Real keys: displayName, enable (bool), staticTags (list). Role has
            # no equivalent at all — staticTags is the nearest thing.
            tags = r.get("staticTags") or []
            tag_labels = [t.get("name", "") if isinstance(t, dict) else str(t) for t in tags]
            if "enable" in r:
                status = "enabled" if r.get("enable") else "disabled"
            else:
                status = r.get("status") or r.get("registrationStatus") or ""
            registrations.append({
                "mac": r.get("macAddress") or r.get("mac") or "",
                "description": r.get("displayName") or r.get("description") or r.get("name") or "",
                "status": status,
                "role": ", ".join(t for t in tag_labels if t) or r.get("role") or r.get("userRole") or "",
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

    # Named VLANs — the tenant's VLAN definitions, which no page surfaces today
    # (the WLANs all bind to VLAN 200, defined here). None = couldn't read;
    # [] = none defined.
    named_vlans = None
    try:
        from vendors.central_bridge import list_named_vlans
        named_vlans = await list_named_vlans()
    except Exception as exc:
        logger.warning("Named VLANs unavailable: %s", exc)

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
            "named_vlans": named_vlans,
            "active": "config",
        },
    )


@router.post("/config/running")
async def running_config(request: Request, serial: str = Form(...)):
    # get_device_running_config's four candidate endpoints all fail on this
    # tenant (400/404), so the old handler dumped a Python dict repr — internal
    # API paths and all — into the page. `show running-config` over the ops CLI
    # is the path that actually works (it is what /lab/config uses). Secrets in
    # the output are masked before they reach the browser.
    from vendors.aruba_central import _norm_device
    from vendors.central_bridge import run_show
    from ops_format import mask_config_secrets

    serial = (serial or "").strip()
    raw = await aruba.get_device(serial)
    if not raw:
        return HTMLResponse("<p style='color:#f87171;'>Device not found.</p>")
    device = _norm_device(raw)
    try:
        result = await run_show(serial, device.get("type") or "", ["show running-config"])
        if isinstance(result, dict) and result.get("errors") and not result.get("output"):
            reason = "; ".join(str(e) for e in result["errors"])[:300]
            return HTMLResponse(
                f"<p style='color:#f87171;'>Could not read the running config: "
                f"{html.escape(reason)}</p>"
            )
        results = (result.get("output") or {}).get("results", []) if isinstance(result, dict) else []
        parts = []
        for item in results:
            body = mask_config_secrets(item.get("output", "") or "")
            if not body.strip():
                continue
            parts.append(
                f"<pre style='font-size:.72rem;color:#94a3b8;white-space:pre-wrap;"
                f"word-break:break-all;'>{html.escape(body)}</pre>"
            )
        if not parts:
            return HTMLResponse("<p style='color:#64748b;'>No configuration returned.</p>")
        return HTMLResponse("".join(parts))
    except Exception:
        logger.exception("Running config fetch failed for %s", serial)
        return HTMLResponse(
            f"<p style='color:#f87171;'>{html.escape(BRIDGE_UNAVAILABLE)}</p>"
        )
