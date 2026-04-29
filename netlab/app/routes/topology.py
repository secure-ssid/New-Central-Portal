from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
import asyncio, json

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def topology(request: Request):
    from vendors.central_bridge import get_devices, get_switch_ports
    from vendors.aruba_central import _norm_device

    raw_devices = await get_devices(limit=100)
    devices = [_norm_device(d) for d in raw_devices]

    # Only show online devices in the topology
    devices = [d for d in devices if (d.get("status") or "").lower() == "online"]

    nodes = []
    edges = []
    seen_edges = set()

    # Build a serial→device map for quick lookup
    by_serial = {d["serial"]: d for d in devices if d["serial"]}

    # Add all device nodes
    for d in devices:
        dtype = d["type"]
        if dtype == "switch":
            group = "switch"
        elif dtype == "access_point":
            group = "ap"
        elif dtype == "gateway":
            group = "gateway"
        else:
            group = "unknown"

        nodes.append({
            "data": {
                "id": d["serial"],
                "label": d["name"] or d["serial"],
                "group": group,
                "model": d["model"],
                "status": d["status"],
                "ip": d["ip"],
                "site": d["site"],
                "url": f"/devices/{d['serial']}",
            }
        })

    # Fetch switch ports to build edges
    switches = [d for d in devices if d["type"] == "switch"]
    port_results = await asyncio.gather(
        *[get_switch_ports(sw["serial"]) for sw in switches],
        return_exceptions=True,
    )

    for sw, ports in zip(switches, port_results):
        if isinstance(ports, Exception) or not ports:
            continue
        for port in ports:
            nbr_serial = port.get("neighbourSerial") or ""
            nbr_type = (port.get("neighbourType") or "").lower()
            if not nbr_serial or nbr_serial == sw["serial"]:
                continue
            # Only wire to known infrastructure devices, not raw client MACs
            if nbr_type not in ("access point", "gateway", "switch"):
                continue
            edge_key = tuple(sorted([sw["serial"], nbr_serial]))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append({
                "data": {
                    "id": f"{sw['serial']}-{nbr_serial}",
                    "source": sw["serial"],
                    "target": nbr_serial,
                    "port": port.get("name") or port.get("id") or "",
                    "speed": port.get("speed") or 0,
                }
            })

    graph_data = json.dumps({"nodes": nodes, "edges": edges})

    return templates.TemplateResponse(
        request,
        "topology.html",
        {"graph_data": graph_data, "active": "topology"},
    )
