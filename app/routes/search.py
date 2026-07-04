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
        site_id = s.get("site_id") or s.get("id") or s.get("siteId") or ""
        name = s.get("site_name") or s.get("siteName") or s.get("name") or ""
        city = s.get("city") or ""
        state = s.get("state") or ""
        address = s.get("address") or ""
        if not _matches(query, name, city, state, address):
            continue
        url = f"/sites/{site_id}" if site_id else "/sites/"
        out.append({
            "type": "site",
            "label": name or "Unnamed site",
            "sublabel": " · ".join(p for p in (city, state) if p) or address,
            "url": url,
            "status": "",
        })
        if len(out) >= PER_TYPE_CAP:
            break
    return out


def search_alerts(query: str, raw_alerts: list) -> list[dict]:
    """Filter active Central alerts for the command palette."""
    out = []
    for a in raw_alerts:
        if not isinstance(a, dict):
            continue
        title = a.get("title") or a.get("alertName") or a.get("name") or ""
        body = a.get("description") or a.get("message") or ""
        device = a.get("deviceName") or a.get("device_name") or ""
        serial = a.get("serialNumber") or a.get("serial") or ""
        site = a.get("siteName") or a.get("site_name") or ""
        if not _matches(query, title, body, device, serial, site):
            continue
        url = f"/devices/{quote(serial, safe='')}" if serial else "/alerts/"
        out.append({
            "type": "alert",
            "label": title or "Alert",
            "sublabel": " · ".join(p for p in (device, site) if p) or "Central alert",
            "url": url,
            "status": str(a.get("severity") or "").lower(),
        })
        if len(out) >= PER_TYPE_CAP:
            break
    return out


def search_wlans(query: str, raw_wlans: list) -> list[dict]:
    """Filter WLAN/SSID inventory for the command palette."""
    out = []
    for w in raw_wlans:
        if not isinstance(w, dict):
            continue
        name = w.get("ssid") or w.get("name") or w.get("wlanName") or w.get("essid") or ""
        sec = w.get("security") or w.get("opmode") or w.get("type") or ""
        if not _matches(query, name, sec):
            continue
        out.append({
            "type": "wlan",
            "label": name or "WLAN",
            "sublabel": sec or "Wireless network",
            "url": "/wlans/",
            "status": "",
        })
        if len(out) >= PER_TYPE_CAP:
            break
    return out


def build_results(query: str, raw_devices: list, raw_clients: list,
                  raw_sites: list, raw_alerts: list | None = None,
                  raw_wlans: list | None = None) -> list[dict]:
    """Combine per-type matches (devices, clients, sites, alerts, wlans) with caps applied."""
    results: list[dict] = []
    sources = (
        (search_devices, raw_devices),
        (search_clients, raw_clients),
        (search_sites, raw_sites),
        (search_alerts, raw_alerts or []),
        (search_wlans, raw_wlans or []),
    )
    for fn, raw in sources:
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
    alerts: list = []
    wlans: list = []
    try:
        from vendors.central_bridge import (
            get_all_clients,
            get_all_devices,
            get_central_sites,
            list_active_alerts,
            list_wlans,
        )

        fetched = await asyncio.gather(
            get_all_devices(max_items=1000),
            get_all_clients(max_items=1000),
            get_central_sites(),
            list_active_alerts(limit=50),
            list_wlans(limit=50),
            return_exceptions=True,
        )
        labelled = zip(("devices", "clients", "sites", "alerts", "wlans"), fetched)
        cleaned = {}
        for label, value in labelled:
            if isinstance(value, BaseException):
                logger.warning("[search] %s fetch failed: %s", label, value)
                cleaned[label] = []
            else:
                cleaned[label] = value if isinstance(value, list) else []
        devices = cleaned["devices"]
        clients = cleaned["clients"]
        sites = cleaned["sites"]
        alerts = cleaned["alerts"]
        wlans = cleaned["wlans"]
    except Exception:
        # central_bridge import failure etc. — degrade to empty results.
        logger.exception("[search] data fetch failed for query %r", query)

    try:
        results = build_results(query, devices, clients, sites, alerts, wlans)
    except Exception:
        logger.exception("[search] result build failed for query %r", query)
        results = []

    return JSONResponse({"results": results})
