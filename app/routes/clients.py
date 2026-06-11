import asyncio
import logging

from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from vendors.aruba_central import aruba

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)

# ── Server-side pagination ────────────────────────────────────────────────────

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200


def _paginate(request: Request, items: list) -> dict:
    """Slice ``items`` for the current request's ``page``/``per_page`` params.

    ``page`` is 1-based (default 1, clamped into range); ``per_page`` defaults
    to 50 and is clamped to 1..200. Invalid/non-numeric values fall back to
    the defaults instead of erroring. Returns the page slice plus
    template-ready metadata, including ``base_qs`` — the current query string
    minus ``page`` — so pagination links preserve every other parameter.
    """
    from urllib.parse import urlencode

    try:
        per_page = int(request.query_params.get("per_page", DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    per_page = max(1, min(MAX_PER_PAGE, per_page))

    try:
        page = int(request.query_params.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    total = len(items)
    total_pages = max(1, -(-total // per_page))  # ceil div, min 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page

    base_qs = urlencode(
        [(k, v) for k, v in request.query_params.multi_items() if k != "page"]
    )
    return {
        "items": items[start:start + per_page],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "base_qs": base_qs,
    }


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

    # For wireless clients, find the switch the AP uplinks through
    # (concurrent + memoized via _resolve_uplinks; a failure just leaves
    # the uplink hop unknown rather than breaking the page).
    uplink = None
    if client.get("type") == "wireless" and client.get("connected_device_serial"):
        ap_serial = client["connected_device_serial"]
        uplinks = await _resolve_uplinks([ap_serial])
        uplink = uplinks.get(ap_serial)

    return templates.TemplateResponse(
        request,
        "clients/detail.html",
        {"client": client, "uplink": uplink, "active": "clients"},
    )
