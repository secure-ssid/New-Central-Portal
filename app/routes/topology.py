from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
import json
import logging

from topology_graph import build_topology_edges

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def topology(request: Request):
    from vendors.aruba_central import _norm_device, aruba

    get_switch_ports = None
    find_device_uplink = None
    try:
        from vendors.central_bridge import (
            find_device_uplink as _uplink,
            get_devices,
            get_switch_ports,
        )
        find_device_uplink = _uplink
        raw_devices = await get_devices(limit=200)
    except Exception:
        logger.warning("central_bridge unavailable for topology, using fallback devices")
        get_switch_ports = None
        find_device_uplink = None
        raw_devices = await aruba.get_devices()

    devices = [_norm_device(d) for d in raw_devices]
    devices = [d for d in devices if d.get("serial")]

    group_map = {
        "switch": "switch",
        "access_point": "ap",
        "gateway": "gateway",
    }

    nodes = []
    for d in devices:
        nodes.append({
            "id": d["serial"],
            "label": d["name"] or d["serial"],
            "group": group_map.get(d["type"], "unknown"),
            "model": d["model"],
            "status": d["status"],
            "ip": d["ip"],
            "site": d["site"],
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
        },
    )
