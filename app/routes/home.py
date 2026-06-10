import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from vendors.aruba_central import aruba

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


@router.get("/")
async def home(request: Request):
    """Dashboard / home page with quick stats."""
    devices, clients = await asyncio.gather(
        aruba.get_devices(),
        aruba.get_clients(),
    )

    sites_count = 0
    try:
        from vendors.central_bridge import get_sites
        sites = await get_sites()
        sites_count = len(sites)
    except Exception as exc:
        logger.warning("Sites count unavailable for dashboard: %s", exc)

    online = [d for d in devices if d.get("status") == "online"]
    offline = [d for d in devices if d.get("status") != "online"]

    stats = {
        "total_devices": len(devices),
        "online_devices": len(online),
        "offline_devices": len(offline),
        "online_pct": int(len(online) / len(devices) * 100) if devices else 0,
        "total_clients": len(clients),
        "switches": sum(1 for d in devices if d.get("type") == "switch"),
        "aps": sum(1 for d in devices if d.get("type") == "access_point"),
        "gateways": sum(1 for d in devices if d.get("type") == "gateway"),
        "sites": sites_count,
    }

    updated = datetime.now(timezone.utc).strftime("%I:%M %p UTC")

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "stats": stats,
            "devices": devices[:10],
            "updated": updated,
            "active": "home",
        },
    )
