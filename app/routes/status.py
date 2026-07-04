"""Portal connectivity and data-source status for UI banners."""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


async def probe_status() -> dict:
    """Probe DB and centralmcp bridge; never raises."""
    import db

    db_ok = db.ping()
    central = "unavailable"
    data_mode = "mock"

    try:
        from vendors.central_bridge import get_devices
        devices = await get_devices(limit=1)
        central = "connected"
        data_mode = "live" if devices else "live"
    except ImportError:
        central = "unavailable"
        data_mode = "mock"
    except Exception as exc:
        logger.debug("Central probe failed: %s", exc)
        central = "error"
        data_mode = "mock"

    if db_ok and central == "connected":
        mode = "live"
        label = "Live data from Aruba Central"
        severity = "ok"
    elif central in ("connected", "error") and not db_ok:
        mode = "partial"
        label = "Partial — database unavailable; alerts and settings may not persist"
        severity = "warn"
    elif central == "unavailable":
        mode = "mock"
        label = "Demo mode — centralmcp not mounted; showing sample data"
        severity = "warn"
    else:
        mode = "degraded"
        label = "Degraded — some integrations are unavailable"
        severity = "warn"

    return {
        "mode": mode,
        "label": label,
        "severity": severity,
        "db": "ok" if db_ok else "fail",
        "central": central,
        "data_mode": data_mode,
    }


@router.get("/api/status")
async def api_status():
    return JSONResponse(await probe_status())
