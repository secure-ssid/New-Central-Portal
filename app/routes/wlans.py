"""WLAN inventory — read-only SSID/WLAN listing via centralmcp."""
import logging

from fastapi import APIRouter, Request

from bridge_errors import BRIDGE_UNAVAILABLE
from pagination import filter_items, paginate as _paginate
from templates_shared import templates

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/")
async def list_wlans_page(request: Request):
    wlans: list[dict] = []
    error = None
    q = request.query_params.get("q", "").strip()
    try:
        from vendors.central_bridge import list_wlans
        raw = await list_wlans(limit=200)
        for w in raw:
            if not isinstance(w, dict):
                continue
            # essid arrives as {"name": "Air Pass"} on this tenant, not a
            # string — rendering the dict put a raw `{'name': ...}` on the page.
            essid = w.get("essid")
            essid_name = essid.get("name", "") if isinstance(essid, dict) else (essid or "")
            profile = w.get("ssid") or ""
            # VLAN is `vlan-id-range: ["200"]`, not `vlan`/`vlanId`.
            vlan_range = w.get("vlan-id-range") or w.get("vlan") or w.get("vlanId") or ""
            vlan = ", ".join(str(v) for v in vlan_range) if isinstance(vlan_range, list) else str(vlan_range or "")
            # The enable flag is `enable` (bool). The old default read a
            # nonexistent `status` key, so it was True unconditionally.
            enabled = w.get("enable", w.get("enabled", w.get("status") != "disabled"))
            wlans.append({
                "name": profile or essid_name or w.get("name") or "",
                "essid": essid_name or profile,
                "type": w.get("type") or w.get("wlanType") or "",
                "security": w.get("opmode") or w.get("security") or "",
                "vlan": vlan,
                "enabled": bool(enabled),
            })
    except Exception as exc:
        logger.warning("WLAN list unavailable: %s", exc)
        error = BRIDGE_UNAVAILABLE

    wlans = filter_items(wlans, q, "name", "essid", "type", "security", "vlan")
    pg = _paginate(request, wlans)

    return templates.TemplateResponse(
        request,
        "wlans/list.html",
        {
            "wlans": pg["items"],
            "error": error,
            "q": q,
            "active": "wlans",
            "page": pg["page"],
            "per_page": pg["per_page"],
            "total": pg["total"],
            "total_pages": pg["total_pages"],
            "has_prev": pg["has_prev"],
            "has_next": pg["has_next"],
            "base_qs": pg["base_qs"],
        },
    )
