"""WLAN inventory — read-only SSID/WLAN listing via centralmcp."""
import logging

from fastapi import APIRouter, Request

router = APIRouter()
from templates_shared import templates
logger = logging.getLogger(__name__)


@router.get("/")
async def list_wlans_page(request: Request):
    wlans: list[dict] = []
    error = None
    try:
        from vendors.central_bridge import list_wlans
        raw = await list_wlans(limit=200)
        for w in raw:
            if not isinstance(w, dict):
                continue
            wlans.append({
                "name": w.get("name") or w.get("ssidName") or w.get("ssid") or "",
                "essid": w.get("essid") or w.get("ssid") or "",
                "type": w.get("type") or w.get("wlanType") or "",
                "security": w.get("security") or w.get("opmode") or "",
                "vlan": w.get("vlan") or w.get("vlanId") or "",
                "enabled": w.get("enabled", w.get("status") != "disabled"),
            })
    except Exception as exc:
        logger.warning("WLAN list unavailable: %s", exc)
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "wlans/list.html",
        {"wlans": wlans, "error": error, "active": "wlans"},
    )
