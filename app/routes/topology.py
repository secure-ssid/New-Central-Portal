from fastapi import APIRouter, Request
import json
import logging

from topology_graph import build_topology_edges
from templates_shared import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_DEVICE_CAP = 200


def _site_name_map(raw_sites: list) -> dict[str, str]:
    """Lowercase site name → site id."""
    mapping: dict[str, str] = {}
    for s in raw_sites:
        if not isinstance(s, dict):
            continue
        site_id = s.get("site_id") or s.get("id") or s.get("siteId") or ""
        name = s.get("site_name") or s.get("siteName") or s.get("name") or ""
        if name and site_id:
            mapping[name.strip().lower()] = str(site_id)
    return mapping


def _resolve_site_query(initial_site: str, site_map: dict[str, str]) -> tuple[str | None, str | None]:
    """Map ``?site=`` query to (display name, site id) for server-side filtering."""
    if not initial_site:
        return None, None
    query = initial_site.strip()
    if query.isdigit():
        site_id = query
        id_to_name = {v: k for k, v in site_map.items()}
        name_key = id_to_name.get(site_id)
        display = name_key.title() if name_key else None
        return display, site_id
    key = query.lower()
    site_id = site_map.get(key)
    return query, site_id


@router.get("/")
async def topology(request: Request):
    from vendors.aruba_central import _norm_device, aruba

    initial_site = (request.query_params.get("site") or "").strip()

    get_switch_ports = None
    find_device_uplink = None
    raw_devices = []
    site_map: dict[str, str] = {}
    site_name_filter: str | None = None
    site_id_filter: str | None = None

    try:
        from vendors.central_bridge import (
            find_device_uplink as _uplink,
            get_central_sites,
            get_devices,
            get_switch_ports,
        )
        find_device_uplink = _uplink
        raw_sites = await get_central_sites()
        if isinstance(raw_sites, Exception):
            raw_sites = []
        site_map = _site_name_map(raw_sites if isinstance(raw_sites, list) else [])
        site_name_filter, site_id_filter = _resolve_site_query(initial_site, site_map)

        if site_id_filter:
            raw_devices = await get_devices(site_id=site_id_filter, limit=_DEVICE_CAP)
        else:
            raw_devices = await get_devices(limit=_DEVICE_CAP)
        if isinstance(raw_devices, Exception):
            raise raw_devices
    except Exception:
        logger.warning("central_bridge unavailable for topology, using fallback devices")
        get_switch_ports = None
        find_device_uplink = None
        raw_devices = await aruba.get_devices()
        site_name_filter, site_id_filter = _resolve_site_query(initial_site, site_map)

    devices = [_norm_device(d) for d in raw_devices]
    devices = [d for d in devices if d.get("serial")]
    if site_name_filter and not site_id_filter:
        name_key = site_name_filter.lower()
        devices = [d for d in devices if (d.get("site") or "").lower() == name_key]
    elif site_name_filter and site_id_filter:
        name_key = site_name_filter.lower()
        devices = [
            d for d in devices
            if (d.get("site") or "").lower() == name_key
            or str(d.get("site_id") or "") == site_id_filter
        ]

    capped = len(devices) >= _DEVICE_CAP and not site_id_filter

    group_map = {
        "switch": "switch",
        "access_point": "ap",
        "gateway": "gateway",
    }

    nodes = []
    for d in devices:
        site_name = d.get("site") or ""
        site_id = site_map.get(site_name.lower()) or d.get("site_id") or ""
        site_url = f"/sites/{site_id}" if site_id else (
            f"/devices/?site={site_name}" if site_name else ""
        )
        nodes.append({
            "id": d["serial"],
            "label": d["name"] or d["serial"],
            "group": group_map.get(d["type"], "unknown"),
            "model": d["model"],
            "status": d["status"],
            "ip": d["ip"],
            "site": site_name,
            "site_id": str(site_id) if site_id else "",
            "site_url": site_url,
            "url": f"/devices/{d['serial']}",
        })
    node_ids = {n["id"] for n in nodes}

    port_fail_count = 0
    if get_switch_ports is not None:
        edges, port_fail_count = await build_topology_edges(
            devices,
            node_ids,
            get_switch_ports,
            find_device_uplink=find_device_uplink,
        )
    else:
        edges = []

    if port_fail_count:
        logger.warning(
            "Port data unavailable for %d switch(es) in topology view",
            port_fail_count,
        )

    online_count = sum(
        1 for n in nodes if (n["status"] or "").lower() == "online"
    )

    graph_json = json.dumps({"nodes": nodes, "edges": edges}).replace("</", "<\\/")

    filter_label = site_name_filter or initial_site or ""

    return templates.TemplateResponse(
        request,
        "topology.html",
        {
            "graph_data": graph_json,
            "active": "topology",
            "device_count": len(nodes),
            "online_count": online_count,
            "offline_count": len(nodes) - online_count,
            "port_fail_count": port_fail_count,
            "initial_site": filter_label,
            "site_filter_active": bool(initial_site),
            "device_cap_hit": capped,
            "device_cap": _DEVICE_CAP,
        },
    )
