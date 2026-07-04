"""Global search API powering the command palette (Ctrl+K / Cmd+K).

GET /search/api?q=...  →  {"results": [{type, label, sublabel, url, status}]}

Searches devices, clients, and sites concurrently via central_bridge.
Designed to never 500 — any backend failure degrades to partial (or empty)
results, with the failure logged.
"""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from search_inventory_cache import get_search_inventory

logger = logging.getLogger(__name__)

router = APIRouter()

PER_TYPE_CAP = 8
OVERALL_CAP = 15
MIN_QUERY_LEN = 2


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


def search_devices(query: str, raw_devices: list, *, cap: int | None = PER_TYPE_CAP) -> list[dict]:
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
        if cap is not None and len(out) >= cap:
            break
    return out


def search_clients(query: str, raw_clients: list, *, cap: int | None = PER_TYPE_CAP) -> list[dict]:
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
        if cap is not None and len(out) >= cap:
            break
    return out


def search_sites(query: str, raw_sites: list, *, cap: int | None = PER_TYPE_CAP) -> list[dict]:
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
        if cap is not None and len(out) >= cap:
            break
    return out


def search_alerts(query: str, raw_alerts: list, *, cap: int | None = PER_TYPE_CAP) -> list[dict]:
    """Filter active Central alerts for the command palette."""
    out = []
    enc = quote(query)
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
        sev = str(a.get("severity") or "").lower()
        url = f"/alerts/?q={enc}"
        if sev in ("critical", "major", "minor"):
            url = f"/alerts/?severity={quote(sev)}&q={enc}"
        out.append({
            "type": "alert",
            "label": title or "Alert",
            "sublabel": " · ".join(p for p in (device, site) if p) or "Central alert",
            "url": url,
            "status": sev,
        })
        if cap is not None and len(out) >= cap:
            break
    return out


def search_wlans(query: str, raw_wlans: list, *, cap: int | None = PER_TYPE_CAP) -> list[dict]:
    """Filter WLAN/SSID inventory for the command palette."""
    out = []
    enc = quote(query)
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
            "url": f"/wlans/?q={enc}",
            "status": "",
        })
        if cap is not None and len(out) >= cap:
            break
    return out


def _view_all_actions(query: str, raw_devices: list, raw_clients: list,
                      raw_sites: list, raw_alerts: list, raw_wlans: list) -> list[dict]:
    """Synthetic palette rows linking to full list pages when matches exceed caps."""
    enc = quote(query)
    actions: list[dict] = []
    checks = (
        ("device", search_devices, raw_devices, f"/devices/?q={enc}", "View all device matches"),
        ("client", search_clients, raw_clients, f"/clients/?q={enc}", "View all client matches"),
        ("site", search_sites, raw_sites, f"/sites/?q={enc}", "View all site matches"),
        ("alert", search_alerts, raw_alerts, f"/alerts/?q={enc}", "View all alert matches"),
        ("wlan", search_wlans, raw_wlans, f"/wlans/?q={enc}", "View all WLAN matches"),
    )
    for kind, fn, raw, url, label in checks:
        try:
            total = len(fn(query, raw, cap=None))
        except Exception:
            continue
        if total > PER_TYPE_CAP:
            actions.append({
                "type": "action",
                "label": label,
                "sublabel": f"{total} matches",
                "url": url,
                "status": "",
            })
    return actions


def build_results(query: str, raw_devices: list, raw_clients: list,
                  raw_sites: list, raw_alerts: list | None = None,
                  raw_wlans: list | None = None) -> list[dict]:
    """Combine per-type matches (devices, clients, sites, alerts, wlans) with caps applied."""
    alerts = raw_alerts or []
    wlans = raw_wlans or []
    results: list[dict] = []
    sources = (
        (search_devices, raw_devices),
        (search_clients, raw_clients),
        (search_sites, raw_sites),
        (search_alerts, alerts),
        (search_wlans, wlans),
    )
    for fn, raw in sources:
        try:
            results.extend(fn(query, raw))
        except Exception:
            logger.exception("[search] %s failed for query %r", fn.__name__, query)
    trimmed = results[:OVERALL_CAP]
    if len(results) >= OVERALL_CAP:
        trimmed.extend(_view_all_actions(query, raw_devices, raw_clients, raw_sites, alerts, wlans)[:2])
    else:
        trimmed.extend(_view_all_actions(query, raw_devices, raw_clients, raw_sites, alerts, wlans))
    # Dedupe action URLs while preserving order
    seen_urls: set[str] = set()
    deduped: list[dict] = []
    for row in trimmed:
        if row.get("type") == "action":
            url = row.get("url") or ""
            if url in seen_urls:
                continue
            seen_urls.add(url)
        deduped.append(row)
    return deduped[:OVERALL_CAP + 3]


@router.get("/api")
async def search_api(q: str = Query("", max_length=200)):
    """Search devices, clients, and sites; degrades to partial results."""
    query = q.strip().lower()
    if not query:
        return JSONResponse({"results": []})
    if len(query) < MIN_QUERY_LEN:
        return JSONResponse({"results": []})

    try:
        inv = await get_search_inventory()
        results = build_results(
            query,
            inv.get("devices", []),
            inv.get("clients", []),
            inv.get("sites", []),
            inv.get("alerts", []),
            inv.get("wlans", []),
        )
    except Exception:
        logger.exception("[search] result build failed for query %r", query)
        results = []

    return JSONResponse({"results": results})
