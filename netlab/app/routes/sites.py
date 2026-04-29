import logging

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


@router.get("/")
async def list_sites(request: Request):
    try:
        from vendors.central_bridge import get_sites
        raw = await get_sites()
        sites = [
            {
                "id": s.get("id", s.get("siteId", "")),
                "name": s.get("siteName", s.get("name", "")),
                "devices": s.get("associated_device_count", s.get("deviceCount", 0)),
                "clients": s.get("client_count", s.get("clientCount", 0)),
                "address": s.get("address", ""),
                "city": s.get("city", ""),
                "state": s.get("state", ""),
            }
            for s in raw
        ]
    except Exception as exc:
        logger.warning("central_bridge unavailable for sites, using mock: %s", exc)
        sites = [{"id": "mem-hq", "name": "Memphis HQ", "devices": 9, "clients": 32}]

    return templates.TemplateResponse(
        request,
        "sites/list.html",
        {"sites": sites, "active": "sites"},
    )
