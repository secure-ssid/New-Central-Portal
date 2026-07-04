import asyncio
import logging

from fastapi import APIRouter, Request, HTTPException
from pagination import paginate as _paginate
from vendors.aruba_central import aruba

from templates_shared import templates

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_uplinks(serials: list[str]) -> dict[str, dict | None]:
    """Resolve AP/device uplink switches concurrently, memoized per serial.

    ``find_device_uplink()`` scans every switch's ports, so calling it once
    per client *sequentially* is an N+1 hot spot. This helper:
      * deduplicates serials within the request (dict cache), so the same AP
        is never resolved twice, and
      * runs the remaining lookups concurrently via ``asyncio.gather``.
    Each lookup is wrapped in try/except so one failure never kills the page.
    """
    cache: dict[str, dict | None] = {}
    unique_serials = [s for s in dict.fromkeys(serials) if s]
    if not unique_serials:
        return cache

    try:
        from vendors.central_bridge import find_device_uplink
    except Exception as exc:
        logger.warning("central_bridge unavailable for uplink lookups: %s", exc)
        return {s: None for s in unique_serials}

    async def _lookup(serial: str) -> dict | None:
        try:
            return await find_device_uplink(serial)
        except Exception as exc:
            logger.warning("Uplink lookup failed for %s: %s", serial, exc)
            return None

    results = await asyncio.gather(*(_lookup(s) for s in unique_serials))
    cache.update(zip(unique_serials, results))
    return cache


@router.get("/")
async def list_clients(request: Request):
    clients = await aruba.get_clients()
    pg = _paginate(request, clients)  # slice after fetch/filter logic
    return templates.TemplateResponse(
        request,
        "clients/list.html",
        {
            "clients": pg["items"],
            "active": "clients",
            "page": pg["page"],
            "per_page": pg["per_page"],
            "total": pg["total"],
            "total_pages": pg["total_pages"],
            "has_prev": pg["has_prev"],
            "has_next": pg["has_next"],
            "base_qs": pg["base_qs"],
        },
    )


@router.get("/{mac}")
async def client_detail(request: Request, mac: str):
    # Try direct lookup first, fall back to list scan
    client = None
    try:
        from vendors.central_bridge import find_client
        raw = await find_client(mac)
        if raw:
            from vendors.aruba_central import _norm_client
            client = _norm_client(raw)
    except Exception as exc:
        logger.warning("Direct client lookup failed for %s: %s", mac, exc)

    if not client:
        clients = await aruba.get_clients()
        client = next((c for c in clients if c.get("mac") == mac), None)

    if not client:
        raise HTTPException(404, "Client not found")

    client_details = None
    locate = None
    roaming = None
    try:
        from vendors.central_bridge import get_client_details, get_client_roaming_history, locate_client
        client_details, locate, roaming = await asyncio.gather(
            get_client_details(mac),
            locate_client(mac),
            get_client_roaming_history(mac, hours=24),
            return_exceptions=True,
        )
        if isinstance(client_details, Exception):
            client_details = None
        if isinstance(locate, Exception):
            locate = None
        if isinstance(roaming, Exception):
            roaming = None
    except Exception as exc:
        logger.debug("Extended client data unavailable for %s: %s", mac, exc)

    # For wireless clients, find the switch the AP uplinks through
    uplink = None
    if client.get("type") == "wireless" and client.get("connected_device_serial"):
        ap_serial = client["connected_device_serial"]
        uplinks = await _resolve_uplinks([ap_serial])
        uplink = uplinks.get(ap_serial)

    return templates.TemplateResponse(
        request,
        "clients/detail.html",
        {
            "client": client,
            "uplink": uplink,
            "client_details": client_details if isinstance(client_details, dict) else None,
            "locate": locate if isinstance(locate, dict) else None,
            "roaming": roaming if isinstance(roaming, dict) else None,
            "active": "clients",
        },
    )
