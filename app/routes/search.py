"""Global search API powering the command palette (Ctrl+K / Cmd+K).

GET /search/api?q=...  →  {"results": [{type, label, sublabel, url, status}]}

Searches devices, clients, and sites concurrently via central_bridge.
Designed to never 500 — any backend failure degrades to partial (or empty)
results, with the failure logged.
"""
import asyncio
import logging
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

PER_TYPE_CAP = 8
OVERALL_CAP = 15


def _matches(query: str, *fields) -> bool:
    """Case-insensitive substring match across any of the given fields."""
    return any(query in str(f).lower() for f in fields if f)


def _device_status(d: dict) -> str:
    return "online" if d.get("status") == "online" else "offline"


def _client_status(c: dict) -> str:
    raw = str(c.get("status") or "").lower()
    if raw in ("connected", "online", "up"):
        return "online"
    if raw in ("disconnected", "offline", "down", "failed"):
        return "offline"
    return ""


def search_devices(query: str, raw_devices: list) -> list[dict]:
    """Filter raw centralmcp device dicts; returns palette result rows."""
    from vendors.aruba_central import _norm_device

    out = []
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        d = _norm_device(raw)
        if not _matches(query, d["name"], d["serial"], d["ip"], d["mac"],
                        d["site"], d["model"]):
            continue
        sublabel = " · ".join(p for p in (d["model"], d["ip"]) if p) or d["serial"]
        out.append({
            "type": "device",
            "label": d["name"] or d["serial"] or "Unknown device",
            "sublabel": sublabel,
            "url": f"/devices/{quote(d['serial'], safe='')}" if d["serial"] else "/devices/",
            "status": _device_status(d),
        })
        if len(out) >= PER_TYPE_CAP:
            break
    return out


def search_clients(query: str, raw_clients: list) -> list[dict]:
    """Filter raw centralmcp client dicts; returns palette result rows."""
    from vendors.aruba_central import _norm_client

    out = []
    for raw in raw_clients:
        if not isinstance(raw, dict):
            continue
        c = _norm_client(raw)
        if not _matches(query, c["hostname"], c["mac"], c["ip"], c["username"],
                        c["connected_to"], c["site"]):
            continue
        sublabel = " · ".join(p for p in (c["mac"], c["ip"]) if p)
        out.append({
            "type": "client",
            "label": c["hostname"] or c["mac"] or "Unknown client",
            "sublabel": sublabel,
            "url": f"/clients/{quote(c['mac'], safe='')}" if c["mac"] else "/clients/",
            "status": _client_status(c),
        })
        if len(out) >= PER_TYPE_CAP:
            break
    return out


def search_sites(query: str, raw_sites: list) -> list[dict]:
    """Filter Classic Central site dicts; returns palette result rows."""
    out = []
    for s in raw_sites:
        if not isinstance(s, dict):
            continue
        name = s.get("site_name") or s.get("siteName") or s.get("name") or ""
        city = s.get("city") or ""
        state = s.get("state") or ""
        address = s.get("address") or ""
        if not _matches(query, name, city, state, address):
            continue
        out.append({
            "type": "site",
            "label": name or "Unnamed site",
            "sublabel": " · ".join(p for p in (city, state) if p) or address,
            "url": "/sites/",
            "status": "",
        })
        if len(out) >= PER_TYPE_CAP:
            break
    return out


def build_results(query: str, raw_devices: list, raw_clients: list,
                  raw_sites: list) -> list[dict]:
    """Combine per-type matches (devices, clients, sites) with caps applied."""
    results: list[dict] = []
    for fn, raw in ((search_devices, raw_devices),
                    (search_clients, raw_clients),
                    (search_sites, raw_sites)):
        try:
            results.extend(fn(query, raw))
        except Exception:
            logger.exception("[search] %s failed for query %r", fn.__name__, query)
    return results[:OVERALL_CAP]


@router.get("/api")
async def search_api(q: str = Query("", max_length=200)):
    """Search devices, clients, and sites; degrades to partial results."""
    query = q.strip().lower()
    if not query:
        return JSONResponse({"results": []})

    devices: list = []
    clients: list = []
    sites: list = []
    try:
        from vendors.central_bridge import get_all_clients, get_all_devices, get_central_sites

        fetched = await asyncio.gather(
            get_all_devices(max_items=1000),
            get_all_clients(max_items=1000),
            get_central_sites(),
            return_exceptions=True,
        )
        labelled = zip(("devices", "clients", "sites"), fetched)
        cleaned = {}
        for label, value in labelled:
            if isinstance(value, BaseException):
                logger.warning("[search] %s fetch failed: %s", label, value)
                cleaned[label] = []
            else:
                cleaned[label] = value if isinstance(value, list) else []
        devices, clients, sites = cleaned["devices"], cleaned["clients"], cleaned["sites"]
    except Exception:
        # central_bridge import failure etc. — degrade to empty results.
        logger.exception("[search] data fetch failed for query %r", query)

    try:
        results = build_results(query, devices, clients, sites)
    except Exception:
        logger.exception("[search] result build failed for query %r", query)
        results = []

    return JSONResponse({"results": results})
