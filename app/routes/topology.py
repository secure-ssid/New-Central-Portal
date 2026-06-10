from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def topology(request: Request):
    from vendors.central_bridge import get_devices, get_switch_ports
    from vendors.aruba_central import _norm_device

    raw_devices = await get_devices(limit=100)
    devices = [_norm_device(d) for d in raw_devices]

    # Keep every device that has a serial; offline devices are rendered
    # dimmed/red in the 3D view rather than hidden.
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

    # Fetch switch ports to build edges. Only query switches that are
    # online — offline ones can't answer anyway.
    switches = [
        d for d in devices
        if d["type"] == "switch" and (d.get("status") or "").lower() == "online"
    ]
    port_results = await asyncio.gather(
        *[get_switch_ports(sw["serial"]) for sw in switches],
        return_exceptions=True,
    )

    edges = []
    seen_edges = set()
    port_fail_count = 0

    for sw, ports in zip(switches, port_results):
        if isinstance(ports, BaseException):
            port_fail_count += 1
            logger.warning(
                "Failed to fetch ports for switch %s (%s): %r",
                sw.get("name") or sw["serial"], sw["serial"], ports,
            )
            continue
        if not ports:
            continue
        for port in ports:
            nbr_serial = (port.get("neighbourSerial") or "").strip()
            nbr_type = (port.get("neighbourType") or "").lower()
            # Skip empties and self-loops
            if not nbr_serial or nbr_serial == sw["serial"]:
                continue
            # Only wire to known infrastructure devices, not raw client MACs
            if nbr_type not in ("access point", "gateway", "switch"):
                continue
            # Don't create edges to devices we don't have a node for
            if nbr_serial not in node_ids:
                continue
            # Dedupe bidirectional reports with a sorted-tuple key
            edge_key = tuple(sorted((sw["serial"], nbr_serial)))
            if edge_key[0] == edge_key[1] or edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({
                "id": f"{sw['serial']}-{nbr_serial}",
                "source": sw["serial"],
                "target": nbr_serial,
                "port": port.get("name") or port.get("id") or "",
                "speed": port.get("speed") or 0,
            })

    if port_fail_count:
        logger.warning(
            "Port data unavailable for %d of %d switches in topology view",
            port_fail_count, len(switches),
        )

    online_count = sum(
        1 for n in nodes if (n["status"] or "").lower() == "online"
    )

    return templates.TemplateResponse(
        request,
        "topology.html",
        {
            "graph_data": json.dumps({"nodes": nodes, "edges": edges}),
            "active": "topology",
            "device_count": len(nodes),
            "online_count": online_count,
            "offline_count": len(nodes) - online_count,
            "port_fail_count": port_fail_count,
        },
    )
