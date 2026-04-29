from fastapi import APIRouter, Request, HTTPException
from fastapi.templating import Jinja2Templates
from vendors.aruba_central import aruba

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def list_clients(request: Request):
    clients = await aruba.get_clients()
    return templates.TemplateResponse(
        request,
        "clients/list.html",
        {"clients": clients, "active": "clients"},
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
    except Exception:
        pass

    if not client:
        clients = await aruba.get_clients()
        client = next((c for c in clients if c.get("mac") == mac), None)

    if not client:
        raise HTTPException(404, "Client not found")

    # For wireless clients, find the switch the AP uplinks through
    uplink = None
    if client.get("type") == "wireless" and client.get("connected_device_serial"):
        try:
            from vendors.central_bridge import find_device_uplink
            uplink = await find_device_uplink(client["connected_device_serial"])
        except Exception:
            pass

    return templates.TemplateResponse(
        request,
        "clients/detail.html",
        {"client": client, "uplink": uplink, "active": "clients"},
    )
