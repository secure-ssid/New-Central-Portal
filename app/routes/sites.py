import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from pagination import filter_items, paginate as _paginate
from vendors.aruba_central import aruba, site_display_name, site_id_of

from templates_shared import templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _health_fields(summary: dict | None) -> list[dict]:
    """Turn a site health summary dict into label/value rows for the template."""
    if not isinstance(summary, dict):
        return []
    skip = {"status", "healthStatus", "summary", "siteId", "siteName", "site_id", "site_name"}
    rows: list[dict] = []
    for key, val in summary.items():
        if key in skip or val in (None, "", [], {}):
            continue
        label = key.replace("_", " ").replace("Id", " ID")
        if isinstance(val, (dict, list)):
            continue
        rows.append({"label": label, "value": str(val)})
    for key in ("status", "healthStatus", "summary"):
        if summary.get(key) not in (None, ""):
            rows.insert(0, {"label": "Overall", "value": str(summary[key])})
            break
    return rows[:8]


def _norm_site(raw: dict) -> dict:
    return {
        "id": site_id_of(raw),
        "name": site_display_name(raw),
        "devices": raw.get("associated_device_count") or raw.get("deviceCount") or 0,
        "clients": raw.get("client_count") or raw.get("clientCount") or 0,
        "address": raw.get("address") or "",
        "city": raw.get("city") or "",
        "state": raw.get("state") or "",
    }


async def _load_sites() -> list[dict]:
    try:
        from vendors.central_bridge import get_sites
        raw = await get_sites()
        return [_norm_site(s) for s in raw if isinstance(s, dict)]
    except Exception as exc:
        # Deliberately NOT a mock site. This used to invent a "Memphis HQ" with
        # 9 devices and 32 clients, which is indistinguishable from real data on
        # a page whose whole job is to tell you what you have — and it appeared
        # precisely when Central was unreachable, i.e. when trusting it was most
        # costly. The template already renders an empty state.
        logger.warning("Sites unavailable from Central: %s", exc)
        return []


@router.get("/")
async def list_sites(request: Request):
    sites = await _load_sites()
    q = request.query_params.get("q", "").strip()
    sites = filter_items(sites, q, "name", "city", "state", "address")
    pg = _paginate(request, sites)
    return templates.TemplateResponse(
        request,
        "sites/list.html",
        {
            "sites": pg["items"],
            "q": q,
            "active": "sites",
            "page": pg["page"],
            "per_page": pg["per_page"],
            "total": pg["total"],
            "total_pages": pg["total_pages"],
            "has_prev": pg["has_prev"],
            "has_next": pg["has_next"],
            "base_qs": pg["base_qs"],
        },
    )


@router.get("/{site_id}")
async def site_detail(request: Request, site_id: str):
    sites = await _load_sites()
    site = next(
        (s for s in sites if str(s.get("id")) == str(site_id) or s.get("name") == site_id),
        None,
    )
    if not site:
        raise HTTPException(404, "Site not found")

    site_name = site.get("name") or ""
    site_location = ", ".join(p for p in (site.get("city"), site.get("state")) if p)

    site_id_str = str(site.get("id")) if site.get("id") else None
    devices_task = aruba.get_devices(site_id=site_id_str)
    clients_task = aruba.get_clients(site_id=site_id_str)
    health_task = None
    try:
        from vendors.central_bridge import get_site_health_summary
        health_task = get_site_health_summary(
            site_id=str(site.get("id")) if site.get("id") else None,
            site_name=site_name or None,
        )
    except Exception:
        health_task = None

    gather_args = [devices_task, clients_task]
    if health_task is not None:
        gather_args.append(health_task)

    results = await asyncio.gather(*gather_args, return_exceptions=True)
    devices_raw = results[0] if not isinstance(results[0], Exception) else []
    clients_raw = results[1] if not isinstance(results[1], Exception) else []
    health_summary = None
    if health_task is not None and len(results) > 2 and not isinstance(results[2], Exception):
        health_summary = results[2]

    devices = [d for d in devices_raw if isinstance(d, dict)]
    clients = [c for c in clients_raw if isinstance(c, dict)]
    if site_id_str is None and site_name:
        devices = [d for d in devices if (d.get("site") or "").lower() == site_name.lower()]
        clients = [c for c in clients if (c.get("site") or "").lower() == site_name.lower()]
    online_count = sum(1 for d in devices if d.get("status") == "online")
    preview_limit = 25

    health_label = None
    if isinstance(health_summary, dict):
        health_label = (
            health_summary.get("status")
            or health_summary.get("healthStatus")
            or health_summary.get("summary")
        )
        if health_label is not None:
            health_label = str(health_label)

    return templates.TemplateResponse(
        request,
        "sites/detail.html",
        {
            "site": site,
            "site_location": site_location,
            "devices": devices[:preview_limit],
            "clients": clients[:preview_limit],
            "device_total": len(devices),
            "client_total": len(clients),
            "preview_limit": preview_limit,
            "online_count": online_count,
            "health_summary": health_summary if isinstance(health_summary, dict) else None,
            "health_label": health_label,
            "health_fields": _health_fields(health_summary if isinstance(health_summary, dict) else None),
            "active": "sites",
        },
    )
