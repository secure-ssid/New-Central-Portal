"""Short-TTL in-memory cache for command-palette inventory snapshots."""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 45.0
_cache: dict[str, tuple[float, dict[str, list]]] = {}
_lock = asyncio.Lock()


async def get_search_inventory() -> dict[str, list]:
    """Return cached devices/clients/sites/alerts/wlans lists for search."""
    async with _lock:
        now = time.monotonic()
        entry = _cache.get("inventory")
        if entry and now - entry[0] < _CACHE_TTL_SECONDS:
            return entry[1]

    snapshot = await _fetch_inventory()
    async with _lock:
        _cache["inventory"] = (time.monotonic(), snapshot)
    return snapshot


def clear_search_inventory_cache() -> None:
    """Test helper — drop cached snapshot."""
    _cache.clear()


async def _fetch_inventory() -> dict[str, list]:
    import asyncio

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
        cleaned: dict[str, list] = {}
        for label, value in labelled:
            if isinstance(value, BaseException):
                logger.warning("[search-cache] %s fetch failed: %s", label, value)
                cleaned[label] = []
            else:
                cleaned[label] = value if isinstance(value, list) else []
        return cleaned
    except Exception:
        logger.exception("[search-cache] inventory fetch failed")
        return {
            "devices": devices,
            "clients": clients,
            "sites": sites,
            "alerts": alerts,
            "wlans": wlans,
        }
