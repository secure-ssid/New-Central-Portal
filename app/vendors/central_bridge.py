"""Bridge to centralmcp tools — async wrappers for FastAPI.

Requires these env vars (set in docker-compose.yml):
  PYTHONPATH=/centralmcp
  CREDS_PATH=/centralmcp/config/credentials.yaml
  CLASSIC_CENTRAL_BASE_URL, CLASSIC_CENTRAL_CLIENT_ID, etc. (Classic Central)

RAG doc search delegates to centralmcp's LanceDB backend by default
(CENTRALMCP_RAG_BACKEND=lancedb). Set CENTRALMCP_RAG_BACKEND=redis plus
OLLAMA_URL when using the optional Redis Stack deployment.
"""
import asyncio
import contextlib
import contextvars
import importlib
import inspect
import json
import logging
import os
import time
import threading
import weakref
from functools import partial, wraps
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ── Classic Central Client ────────────────────────────────────────────────────

class ClassicCentralClient:
    """Lightweight OAuth2 client for the Classic Central gateway."""

    def __init__(self):
        missing = [
            v for v in (
                "CLASSIC_CENTRAL_BASE_URL",
                "CLASSIC_CENTRAL_CLIENT_ID",
                "CLASSIC_CENTRAL_CLIENT_SECRET",
            )
            if not os.environ.get(v)
        ]
        if missing:
            raise RuntimeError(
                "Classic Central is not configured — missing env vars: "
                + ", ".join(missing)
            )
        self.base_url = os.environ["CLASSIC_CENTRAL_BASE_URL"].rstrip("/")
        self.client_id = os.environ["CLASSIC_CENTRAL_CLIENT_ID"]
        self.client_secret = os.environ["CLASSIC_CENTRAL_CLIENT_SECRET"]
        self._access_token = os.environ.get("CLASSIC_CENTRAL_ACCESS_TOKEN", "")
        self._refresh_token = os.environ.get("CLASSIC_CENTRAL_REFRESH_TOKEN", "")
        self._expires_at: float = time.time() + 7000  # assume ~2h from startup
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        """Refresh the OAuth2 access token."""
        if not self._refresh_token:
            raise RuntimeError(
                "Classic Central access token expired and no "
                "CLASSIC_CENTRAL_REFRESH_TOKEN is configured."
            )
        r = requests.post(
            f"{self.base_url}/oauth2/token",
            json={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._refresh_token,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + data.get("expires_in", 7200) - 120

    def _ensure_token(self) -> str:
        with self._lock:
            if time.time() >= self._expires_at:
                self._refresh()
            return self._access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        return requests.get(f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=30)

    def post(self, path: str, payload: dict | None = None) -> requests.Response:
        return requests.post(f"{self.base_url}{path}", headers=self._headers(), json=payload, timeout=30)


_classic_client: ClassicCentralClient | None = None


def get_classic_client() -> ClassicCentralClient:
    global _classic_client
    if _classic_client is None:
        _classic_client = ClassicCentralClient()
    return _classic_client


# Ceiling on simultaneous upstream calls. Central rate-limits per second and
# answers a 429 with a blocking sleep, so an unbounded fan-out is actively
# counter-productive: parallelising the dashboard cut its round-trip chains but
# pushed the burst over the limit, trading latency for 429 stalls. Six keeps the
# widgets overlapping while staying under the limiter. It also caps how much of
# the default thread-pool executor (16 workers, shared process-wide) the
# synchronous centralmcp calls can occupy.
_MAX_CONCURRENT_UPSTREAM = int(os.environ.get("CENTRAL_MAX_CONCURRENCY", "6"))

# Per-loop: the alerting scheduler runs its own short-lived loop per sweep, and
# an asyncio.Semaphore binds to the loop that first awaits it. Weak keys so a
# closed loop does not leak.
_upstream_semaphores: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _upstream_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _upstream_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENT_UPSTREAM)
        _upstream_semaphores[loop] = sem
    return sem


async def _run(fn, *args, **kwargs) -> Any:
    """Run a centralmcp function — await coroutines, thread-pool sync callables."""
    async with _upstream_semaphore():
        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


# ── Short-TTL response cache ──────────────────────────────────────────────────
#
# Every page route funnels through this module, and none of them used to cache
# anything: a single dashboard render fanned out to ~40 upstream calls, and an
# idle portal with a couple of tabs open produced ~700 calls/hour for a 13-device
# tenant. That is enough to trip Central's rate limiter, and centralmcp answers a
# 429 with a blocking sleep — which is what turned dashboard loads into 60s+
# stalls.
#
# Three mechanisms, all needed:
#   * TTL cache — repeat reads inside the window are free.
#   * In-flight coalescing — concurrent callers for the same key share one
#     upstream request instead of each firing their own. The dashboard used to
#     issue five byte-identical config-health requests within the same
#     millisecond; coalescing collapses those to one.
#   * Stale-while-revalidate — once an entry expires, the next caller is served
#     the stale value immediately and the refresh happens behind the response.
#     Without this, every request that lands just after the TTL lapses pays the
#     full cold cost, so casual browsing kept hitting multi-second loads. Only a
#     genuinely cold key (never fetched, or older than the stale grace) blocks.

_CACHE_TTL_SECONDS = 60.0
# How long past the TTL an entry may still be served while it refreshes.
_STALE_GRACE_SECONDS = 600.0
# Empty and error-shaped payloads stay "fresh" for only a few seconds.
# centralmcp converts upstream failures into ordinary empty values rather than
# raising — MCPClient.get_devices_page returns ([], None) on any exception — so
# a 429 arrives here indistinguishable from "the tenant genuinely has 0 devices".
# Caching that for the full minute blanked the whole portal off a single
# transient failure, which is precisely the condition this cache exists for.
# They keep the normal stale grace, though: plenty of empties are genuine on a
# small tenant, and making those block on a live fetch every time reintroduced
# exactly the multi-second page loads this cache removes.
_LOW_CONFIDENCE_TTL_SECONDS = 5.0
_MAX_CACHE_ENTRIES = 512

# key -> (fresh_until, stale_until, value); both monotonic deadlines
_response_cache: dict[tuple, tuple[float, float, Any]] = {}
# Strong refs to background refresh tasks so they are not garbage collected.
_background_refreshes: set = set()
# key -> (owning_event_loop, future). The alerting scheduler runs its own
# short-lived loop (notifications._fetch_devices_sync), so a future is only
# shareable with callers on the same loop.
_inflight: dict[tuple, tuple[Any, "asyncio.Future"]] = {}
_cache_lock = threading.Lock()

# Set inside a `fresh_data()` block to skip the cache. A ContextVar rather than a
# keyword argument so it propagates through nested calls automatically — e.g.
# get_all_devices -> _fetch_paginated -> get_devices.
_bypass_cache: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "central_bridge_bypass_cache", default=False
)


@contextlib.contextmanager
def fresh_data():
    """Bypass the response cache for everything called inside the block.

    Used by the device-down alerting sweep, which must not inherit up to a
    minute of staleness from whatever a browser tab happened to render.
    """
    token = _bypass_cache.set(True)
    try:
        yield
    finally:
        _bypass_cache.reset(token)


async def warm_cache() -> None:
    """Populate the hot keys so the first visitor is not the one who pays.

    Each uvicorn worker has its own cache, so a cold worker still owed one slow
    render per key. Stale-while-revalidate keeps every *later* request fast, but
    only once something has been fetched at least once — this does that at
    startup, off the request path. Best effort: failures are logged and ignored,
    and a failed warm simply leaves the key cold.
    """
    async def _quiet(label, coro):
        try:
            return await coro
        except Exception as exc:
            logger.debug("Cache warm (%s) failed: %s", label, exc)
            return None

    started = time.monotonic()

    # Round 1 — the inventory every page needs.
    devices, _clients, sites, _alerts = await asyncio.gather(
        _quiet("devices", get_devices()),
        _quiet("clients", get_clients()),
        _quiet("sites", get_sites()),
        _quiet("alerts", list_active_alerts()),
    )

    # Round 2 — the dashboard's expensive widgets, which are what actually made
    # a cold render take seconds. get_site_health_summary alone is ~10 sequential
    # upstream calls per site inside centralmcp. Mirrors routes/home.py's hot
    # set; if it drifts the only cost is a slower first render, not a defect.
    jobs = [_quiet("tenant-health", get_tenant_health()),
            _quiet("fleet-health", get_fleet_health())]

    for site in (sites or [])[:4]:
        if not isinstance(site, dict):
            continue
        site_id = site.get("id") or site.get("siteId") or site.get("site_id")
        name = site.get("siteName") or site.get("site_name") or site.get("name") or ""
        jobs.append(_quiet("site-health", get_site_health_summary(
            site_id=str(site_id) if site_id else None, site_name=name or None)))

    online = [d for d in (devices or [])
              if isinstance(d, dict) and (d.get("serialNumber") or d.get("serial"))]
    for dev in online[:5]:
        serial = dev.get("serialNumber") or dev.get("serial")
        jobs.append(_quiet("events", get_device_events(
            serial,
            site_id=str(dev.get("siteId") or "") or None,
            device_type=dev.get("deviceType") or None,
        )))

    await asyncio.gather(*jobs)
    logger.info("Central response cache warmed in %.1fs", time.monotonic() - started)


def clear_bridge_cache() -> None:
    """Drop all cached responses. Test helper and manual-refresh hook."""
    with _cache_lock:
        _response_cache.clear()
        _inflight.clear()
    with _event_memo_lock:
        _event_memo.clear()
        _event_inflight.clear()


# ── Event-fetch memo (installed onto centralmcp's client) ─────────────────────
#
# Several centralmcp tools fetch the same events independently: on one dashboard
# render detect_client_flapping and detect_ssh_brute_force each called
# MCPClient.get_events(serial, hours=24, api_limit=1000) with identical
# arguments, and each of those internally does a device-inventory lookup to
# re-resolve site + type. That is four upstream requests where one suffices —
# and /network-troubleshooting/v1/events is the endpoint Central rate-limits
# first. Caching in central_bridge cannot help, because these calls never pass
# through it; the memo has to sit on the client itself.
#
# get_events is a pure read, so a short memo is safe. Installed once at startup
# by main.py and guarded so an upstream signature change disables it rather than
# breaking event fetching.

_event_memo: dict[tuple, tuple[float, Any]] = {}
_event_inflight: dict[tuple, threading.Event] = {}
_event_memo_lock = threading.Lock()
_event_memo_installed = False
_EVENT_MEMO_WAIT_SECONDS = 15.0


def install_event_fetch_memo(ttl: float = _CACHE_TTL_SECONDS) -> bool:
    """Wrap MCPClient.get_events with a short TTL memo. Returns True if applied."""
    global _event_memo_installed
    if _event_memo_installed:
        return True
    try:
        from pipeline.clients.mcp_client import MCPClient
    except Exception:
        logger.debug("centralmcp MCPClient not importable — event memo skipped")
        return False

    original = getattr(MCPClient, "get_events", None)
    if original is None or getattr(original, "_portal_memoized", False):
        return original is not None

    @wraps(original)
    def get_events(self, serial_number, *args, **kwargs):
        if _bypass_cache.get():
            return original(self, serial_number, *args, **kwargs)
        try:
            key = (id(self), serial_number, args, tuple(sorted(kwargs.items())))
            hash(key)
        except TypeError:
            return original(self, serial_number, *args, **kwargs)

        now = time.monotonic()
        # These run in thread-pool workers, and the duplicate calls are
        # concurrent (both detectors fire at once for the same device), so a
        # plain TTL lookup would let every one of them miss. Coalesce with an
        # Event: the first caller fetches, the rest wait for its result.
        with _event_memo_lock:
            entry = _event_memo.get(key)
            if entry is not None and (now - entry[0]) < ttl:
                return entry[1]
            waiter = _event_inflight.get(key)
            owner = waiter is None
            if owner:
                waiter = threading.Event()
                _event_inflight[key] = waiter

        if not owner:
            # Bounded: if the owner dies or is starved, fetch rather than hang.
            waiter.wait(timeout=_EVENT_MEMO_WAIT_SECONDS)
            with _event_memo_lock:
                entry = _event_memo.get(key)
            if entry is not None and (time.monotonic() - entry[0]) < ttl:
                return entry[1]
            return original(self, serial_number, *args, **kwargs)

        try:
            result = original(self, serial_number, *args, **kwargs)
        finally:
            with _event_memo_lock:
                _event_inflight.pop(key, None)
            waiter.set()

        with _event_memo_lock:
            _event_memo[key] = (time.monotonic(), result)
            if len(_event_memo) > _MAX_CACHE_ENTRIES:
                cutoff = time.monotonic() - ttl
                for k in [k for k, (ts, _) in _event_memo.items() if ts < cutoff]:
                    _event_memo.pop(k, None)
        return result

    get_events._portal_memoized = True
    MCPClient.get_events = get_events
    _event_memo_installed = True
    logger.info("Installed %.0fs memo on centralmcp MCPClient.get_events", ttl)
    return True


def _prune_locked(now: float) -> None:
    """Evict fully-expired entries. Caller must hold _cache_lock."""
    if len(_response_cache) <= _MAX_CACHE_ENTRIES:
        return
    for key in [k for k, (_, stale_until, _v) in _response_cache.items() if now >= stale_until]:
        _response_cache.pop(key, None)


def _is_low_confidence(result: Any) -> bool:
    """True if this payload might be an upstream failure rather than real data.

    centralmcp does not raise on upstream errors — MCPClient.get_devices_page
    catches Exception and returns ([], None), get_events returns [], and the
    monitoring tools return an envelope carrying an "errors" list. All of those
    reach the cache looking exactly like a legitimate empty result, so the only
    safe move is to keep them briefly and without a stale grace.
    """
    if result is None:
        return True
    if isinstance(result, (list, tuple, dict, str)) and len(result) == 0:
        return True
    if isinstance(result, dict) and result.get("errors"):
        return True
    return False


def _swallow_future_exception(fut: "asyncio.Future") -> None:
    """Mark a future's exception retrieved so asyncio does not log it.

    Real awaiters still receive the exception; this only suppresses the
    "exception was never retrieved" warning when every waiter has gone away.
    """
    if not fut.cancelled():
        fut.exception()


_MISSING = object()


async def _fetch_and_store(fn, args, kwargs, key, loop, fut, ttl):
    """Run the fetcher, publish the result to the cache and to any waiters."""
    try:
        result = await fn(*args, **kwargs)
    except BaseException as exc:
        with _cache_lock:
            if _inflight.get(key) == (loop, fut):
                _inflight.pop(key, None)
        if not fut.done():
            fut.set_exception(exc)
        raise

    now = time.monotonic()
    # A payload that might really be an upstream failure stays "fresh" for only
    # a few seconds, so the portal picks up Central's recovery almost at once
    # instead of showing an empty fleet for a full minute. It still keeps the
    # normal stale grace: many empties are genuine on a small tenant (no
    # anomalies, no events for a quiet AP), and dropping the grace made every
    # one of them block on a live fetch — which is the cost this whole cache
    # exists to remove.
    fresh_for = _LOW_CONFIDENCE_TTL_SECONDS if _is_low_confidence(result) else ttl
    fresh_until = now + fresh_for
    stale_until = fresh_until + _STALE_GRACE_SECONDS

    with _cache_lock:
        _response_cache[key] = (fresh_until, stale_until, result)
        if _inflight.get(key) == (loop, fut):
            _inflight.pop(key, None)
        _prune_locked(now)
    if not fut.done():
        fut.set_result(result)
    return result


async def _refresh_quietly(fn, args, kwargs, key, loop, fut, ttl):
    """Background revalidation — the caller already has a stale value."""
    try:
        await _fetch_and_store(fn, args, kwargs, key, loop, fut, ttl)
    except Exception as exc:
        logger.debug("Background refresh of %s failed: %s", key[0], exc)


def _cached(ttl: float = _CACHE_TTL_SECONDS):
    """Memoize an async fetcher, coalescing concurrent calls.

    Fresh hit -> return immediately. Stale hit -> return immediately and
    revalidate behind the response. Cold -> fetch, sharing one request between
    every concurrent caller on this loop.
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            key: tuple | None = (fn.__name__, args, tuple(sorted(kwargs.items())))
            try:
                hash(key)
            except TypeError:
                # Unhashable argument — correctness beats caching.
                key = None
            if key is None or _bypass_cache.get():
                return await fn(*args, **kwargs)

            loop = asyncio.get_running_loop()
            now = time.monotonic()

            share: "asyncio.Future | None" = None
            stale = _MISSING
            fut = None
            with _cache_lock:
                entry = _response_cache.get(key)
                if entry is not None:
                    fresh_until, stale_until, value = entry
                    if now < fresh_until:
                        return value
                    if now < stale_until:
                        stale = value
                pending = _inflight.get(key)
                if pending is not None and pending[0] is loop:
                    # Someone is already fetching this key.
                    if stale is not _MISSING:
                        return stale
                    share = pending[1]
                else:
                    fut = loop.create_future()
                    fut.add_done_callback(_swallow_future_exception)
                    _inflight[key] = (loop, fut)
            # Never await while holding _cache_lock — it is a threading.Lock and
            # is taken from scheduler threads too.
            if share is not None:
                return await asyncio.shield(share)

            if stale is not _MISSING:
                task = loop.create_task(
                    _refresh_quietly(fn, args, kwargs, key, loop, fut, ttl)
                )
                _background_refreshes.add(task)
                task.add_done_callback(_background_refreshes.discard)
                return stale

            return await _fetch_and_store(fn, args, kwargs, key, loop, fut, ttl)

        return wrapper

    return decorator


def _resolve_troubleshoot_type(serial: str, device_type: str) -> str | None:
    """Map portal device types to centralmcp troubleshoot URL segments."""
    from mcp_servers.shared import device_type_for_troubleshoot

    portal_dt = (device_type or "").lower()
    # Generic "switch" from aruba_central normalization — disambiguate via inventory.
    if portal_dt in ("switch", "unknown", ""):
        return device_type_for_troubleshoot(serial, None)
    return device_type_for_troubleshoot(serial, device_type)


def _ops_error(message: str) -> dict:
    return {"status": None, "errors": [message]}


def _unwrap(result: list | dict | None, *keys: str) -> list[dict]:
    """Flatten paginated centralmcp responses to a plain list.

    ``keys`` names collection keys tried before the usual "items" — e.g.
    ``_unwrap(result, "wlan-ssid")`` for mcp_servers.config.list_ssids, which
    does not use "items". Keys are explicit on purpose: a "first list value
    found" fallback would happily return the ``errors`` list that most
    centralmcp tools include, silently turning an error envelope into data.
    """
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in (*keys, "items"):
            value = result.get(key)
            if isinstance(value, list):
                return value
        logger.warning(
            "centralmcp response had no list under %s — keys present: %s",
            (*keys, "items"), sorted(result)[:8],
        )
        return []
    if result is not None:
        logger.warning("Unexpected centralmcp response shape: %s", type(result).__name__)
    return []


async def _fetch_paginated(fetch_fn, *, limit: int = 200, max_items: int = 1000) -> list[dict]:
    """Page through a centralmcp list tool until exhausted or max_items reached."""
    collected: list[dict] = []
    offset = 0
    while len(collected) < max_items:
        page = _unwrap(await fetch_fn(limit=limit, offset=offset))
        if not page:
            break
        collected.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return collected[:max_items]


# ── Sites ─────────────────────────────────────────────────────────────────────

@_cached()
async def get_sites(limit: int = 100) -> list[dict]:
    from mcp_servers.monitoring import list_sites
    return _unwrap(await _run(list_sites, limit=limit))


@_cached()
async def find_site(name: str) -> dict | None:
    from mcp_servers.monitoring import get_site
    return await _run(get_site, name=name)


# ── Devices ──────────────────────────────────────────────────────────────────

@_cached()
async def get_devices(
    device_type: str | None = None,
    site_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    from mcp_servers.monitoring import list_devices
    kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
    if device_type:
        kwargs["device_type"] = device_type
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_devices, **kwargs))


@_cached()
async def get_all_devices(limit_per_page: int = 200, max_items: int = 1000) -> list[dict]:
    async def _page(**kwargs):
        return await get_devices(**kwargs)
    return await _fetch_paginated(_page, limit=limit_per_page, max_items=max_items)


@_cached()
async def get_device(serial: str) -> dict | None:
    from mcp_servers.monitoring import find_device
    return await _run(find_device, serial_number=serial)


@_cached()
async def get_switch_ports(serial: str) -> list[dict]:
    from mcp_servers.monitoring import list_switch_ports
    result = await _run(list_switch_ports, serial)
    # list_switch_ports returns {"interfaces": None} on total failure — the
    # key exists, so .get(key, []) would leak None to list-typed callers.
    return (result.get("interfaces") or []) if isinstance(result, dict) else []


# These two used to carry their own hand-rolled TTL dicts for uplink
# resolution. They now ride the module-wide cache above, which has the same
# 60s window plus in-flight coalescing — find_device_uplink scans every
# switch's ports per call, so O(APs x switches) callers collapse onto one fetch.


async def _get_switches_cached() -> list[dict]:
    """List switch devices (uplink resolution only)."""
    all_devices = _unwrap(await _list_devices_page(limit=200))
    return [
        d for d in all_devices
        if isinstance(d, dict)
        and (d.get("deviceType") or "").upper() in ("SWITCH", "AOS_S", "AOS-S", "CX", "AOS_CX")
    ]


@_cached()
async def _list_devices_page(limit: int = 200) -> Any:
    from mcp_servers.monitoring import list_devices
    return await _run(list_devices, limit=limit)


async def _get_switch_ports_cached(serial: str) -> list[dict]:
    """get_switch_ports is itself cached now; kept as a named call site."""
    return await get_switch_ports(serial)


@_cached()
async def find_device_uplink(device_serial: str) -> dict | None:
    """Return the switch + port that an AP/device uplinks through."""
    switches = await _get_switches_cached()
    for sw in switches:
        sw_serial = sw.get("serialNumber") or sw.get("serial") or ""
        if not sw_serial:
            continue
        try:
            ports = await _get_switch_ports_cached(sw_serial)
            for port in ports:
                if isinstance(port, dict) and port.get("neighbourSerial") == device_serial:
                    return {
                        "switch_serial": sw_serial,
                        "switch_name": sw.get("deviceName") or sw.get("name") or sw_serial,
                        "port": port.get("name") or port.get("id") or "",
                    }
        except Exception as exc:
            logger.warning(
                "Uplink scan: failed to list ports for switch %s: %s", sw_serial, exc
            )
            continue
    return None


def _event_context_type(device_type: str | None) -> str:
    """Map a device type to Central's troubleshooting event context-type.

    Mirrors centralmcp's private _inventory_device_type_to_event_context;
    reimplemented rather than imported so an upstream rename can't break the
    fast path. Accepts both raw inventory types (ACCESS_POINT, AOS_CX) and the
    portal's normalized ones (access_point, switch, gateway).
    """
    raw = (device_type or "").upper()
    if "ACCESS_POINT" in raw or "IAP" in raw or raw == "AP":
        return "ACCESS_POINT"
    if "GATEWAY" in raw or raw in ("GW", "VPNC"):
        return "GATEWAY"
    return "SWITCH"


@_cached()
async def get_device_events(
    serial: str,
    hours: int = 24,
    limit: int = 20,
    site_id: str | None = None,
    device_type: str | None = None,
) -> list[dict]:
    """Recent events for one device.

    Pass ``site_id`` and ``device_type`` when the caller already has them (any
    device list from Central carries both). centralmcp's list_events tool takes
    neither, so it re-resolves them with an extra device-inventory lookup per
    call — that doubling accounted for ~145 upstream calls an hour on its own.
    """
    if site_id and device_type:
        try:
            from mcp_servers.shared import get_mcp_client
            events = await _run(
                get_mcp_client().get_events,
                serial,
                hours=hours,
                site_id=str(site_id),
                context_type=_event_context_type(device_type),
            )
            if isinstance(events, list):
                return events[:limit]
        except Exception as exc:
            logger.debug(
                "Direct event fetch failed for %s, falling back to list_events: %s",
                serial, exc,
            )

    from mcp_servers.monitoring import list_events
    result = await _run(list_events, serial_number=serial, hours=hours, limit=limit)
    return _unwrap(result) if isinstance(result, dict) else (result if isinstance(result, list) else [])


@_cached()
async def get_device_health(serial: str) -> dict:
    from mcp_servers.monitoring import get_device_health as _health
    return await _run(_health, serial_number=serial)


@_cached()
async def get_fleet_health() -> list[dict]:
    """Fetch the tenant-wide config-health list in one request.

    centralmcp's get_device_health(serial_number=...) does not filter server
    side — it GETs the parameterless /config-health/devices collection and
    filters in Python. Asking per device therefore issued N byte-identical
    requests (66 in one sampled hour) for a list it could have fetched once.
    """
    from mcp_servers.monitoring import get_device_health as _health
    result = await _run(_health)
    items = result.get("health") if isinstance(result, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def health_entry_for(fleet_health: list[dict], serial: str) -> dict | None:
    """Pick one device's entry out of get_fleet_health()'s list.

    Returns None when the device is absent. Note centralmcp falls back to
    returning the *entire* list on a miss, so a per-device lookup there could
    label a device with some other device's health; matching strictly here
    avoids that.
    """
    want = (serial or "").lower()
    if not want:
        return None
    for entry in fleet_health:
        found = (entry.get("serial") or entry.get("serialNumber") or "").lower()
        if found == want:
            return entry
    return None


@_cached()
async def get_lldp_neighbors(serial: str) -> dict:
    from mcp_servers.ops import get_lldp_neighbors as _lldp
    return await _run(_lldp, serial)


@_cached()
async def get_site_health_summary(site_id: str | None = None, site_name: str | None = None) -> dict:
    from mcp_servers.monitoring import get_site_health_summary as _summary
    kwargs: dict = {}
    if site_id:
        kwargs["site_id"] = site_id
    if site_name:
        kwargs["site_name"] = site_name
    return await _run(_summary, **kwargs)


@_cached()
async def get_tenant_health() -> dict:
    from mcp_servers.monitoring import get_tenant_health as _health
    return await _run(_health)


@_cached()
async def get_client_details(mac_address: str) -> dict:
    from mcp_servers.monitoring import get_client_details
    return await _run(get_client_details, mac_address=mac_address)


@_cached()
async def locate_client(mac_address: str) -> dict:
    from mcp_servers.monitoring import locate_client as _locate
    return await _run(_locate, mac_address=mac_address)


@_cached()
async def get_client_roaming_history(mac_address: str, hours: int = 24) -> dict:
    from mcp_servers.monitoring import get_client_roaming_history as _roam
    return await _run(_roam, mac_address=mac_address, hours=hours)


@_cached()
async def list_active_alerts(limit: int = 50) -> list[dict]:
    from mcp_servers.monitoring import list_active_alerts
    result = await _run(list_active_alerts, limit=limit)
    if isinstance(result, dict):
        return result.get("items", result.get("alerts", [])) or []
    return result if isinstance(result, list) else []


async def run_traceroute(serial: str, device_type: str, destination: str) -> dict:
    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "cx":
        from mcp_servers.ops import cx_traceroute
        return await _run(cx_traceroute, serial, destination)
    if dtype == "aos-s":
        from mcp_servers.ops import aos_s_traceroute
        return await _run(aos_s_traceroute, serial, destination)
    if dtype == "aps":
        from mcp_servers.ops import ap_traceroute
        return await _run(ap_traceroute, serial, destination)
    return _ops_error(f"Traceroute is not supported for this device type on {serial}.")


async def get_switch_port_errors(serial: str, interface: str | None = None) -> dict:
    from mcp_servers.ops import get_switch_port_errors as _fn
    return await _run(_fn, serial, interface=interface)


async def find_mac_on_switch(serial: str, mac_address: str) -> dict:
    from mcp_servers.ops import find_mac_on_switch as _fn
    return await _run(_fn, serial, mac_address)


async def get_cx_mac_table(serial: str, interface: str | None = None) -> dict:
    from mcp_servers.ops import get_cx_mac_table as _fn
    return await _run(_fn, serial, interface=interface)


@_cached()
async def get_wireless_metrics(serial: str) -> dict:
    from mcp_servers.monitoring import get_wireless_metrics as _fn
    return await _run(_fn, serial)


@_cached()
async def get_ap_radios(serial: str) -> dict:
    from mcp_servers.monitoring import get_ap_radios as _fn
    return await _run(_fn, serial)


@_cached()
async def get_channel_utilization(serial: str) -> dict:
    from mcp_servers.monitoring import get_channel_utilization as _fn
    return await _run(_fn, serial)


@_cached()
async def get_ap_rf_neighbors(serial: str) -> list[dict]:
    """Co-channel / RF neighbor APs for an access point (best-effort)."""
    try:
        from mcp_servers.monitoring import get_ap_neighbors as _fn
        result = await _run(_fn, serial)
    except Exception:
        result = await invoke_tool_router(
            "get_ap_neighbors", {"serial_number": serial}
        )
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        if result.get("error"):
            return []
        items = result.get("neighbors") or result.get("items") or result.get("aps") or []
        return [r for r in items if isinstance(r, dict)] if isinstance(items, list) else []
    return []


@_cached()
async def detect_client_flapping(serial: str, hours: int = 24) -> dict:
    from mcp_servers.monitoring import detect_client_flapping as _fn
    return await _run(_fn, serial, hours=hours)


@_cached()
async def detect_ssh_brute_force(serial: str, hours: int = 24) -> dict:
    from mcp_servers.monitoring import detect_ssh_brute_force as _fn
    return await _run(_fn, serial, hours=hours)


@_cached()
async def list_wlans(limit: int = 50) -> list[dict]:
    from mcp_servers.config import list_ssids
    # list_ssids returns {"wlan-ssid": [...]}, not the usual {"items": [...]} —
    # reading only "items" is why the WLAN page rendered "No WLANs".
    return _unwrap(await _run(list_ssids, limit=limit), "wlan-ssid")


@_cached()
async def get_firmware_compliance(limit: int = 50) -> dict:
    """Fleet firmware table from /network-services/v1alpha1/firmware-details.

    centralmcp v0.4.0 changed its get_firmware_compliance into a per-scope
    POLICY read (scope_id + device_function required) — no longer a fleet
    listing. The portal's compliance table wants per-device rows, so query
    the underlying firmware-details endpoint directly and synthesize the
    complianceStatus the normalizer expects.
    """
    from mcp_servers.shared import get_client

    def _fetch() -> dict:
        result = get_client().get("/network-services/v1alpha1/firmware-details")
        items = result.get("items", []) if isinstance(result, dict) else []
        for it in items:
            if isinstance(it, dict) and "complianceStatus" not in it:
                current = it.get("firmwareVersion") or it.get("softwareVersion") or ""
                target = it.get("recommendedVersion") or ""
                it["complianceStatus"] = (
                    "compliant" if (not target or current == target) else "non-compliant"
                )
        return {"items": items[:limit]}

    return await _run(_fetch)


async def get_device_running_config(serial: str) -> dict:
    from mcp_servers.config import get_device_running_config as _fn
    return await _run(_fn, serial)


@_cached()
async def list_mac_registrations(limit: int = 50, offset: int = 0) -> list[dict]:
    from mcp_servers.nac import list_mac_registrations as _fn
    result = await _run(_fn, limit=limit, offset=offset)
    return _unwrap(result) if isinstance(result, dict) else (result if isinstance(result, list) else [])


@_cached()
async def list_glp_service_offers(limit: int = 50) -> list[dict]:
    from mcp_servers.glp import list_glp_service_offers
    result = await _run(list_glp_service_offers, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


async def invoke_tool_router(tool_name: str, params: dict) -> dict:
    """Fallback dispatch via centralmcp tool_router when not in _TOOL_MAP."""
    try:
        from mcp_servers.tool_router import invoke_read_tool
        # v0.4.0 signature: invoke_read_tool(ctx, name, arguments). ctx is only
        # used for FastMCP context injection on ctx-requiring (write) tools —
        # read-only dispatch tolerates None.
        return await _run(invoke_read_tool, None, name=tool_name, arguments=params)
    except Exception as exc:
        return {"error": str(exc)}


# ── Clients ──────────────────────────────────────────────────────────────────

@_cached()
async def get_clients(
    site_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    from mcp_servers.monitoring import list_clients
    kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_clients, **kwargs))


@_cached()
async def get_all_clients(limit_per_page: int = 200, max_items: int = 1000) -> list[dict]:
    async def _page(**kwargs):
        return await get_clients(**kwargs)
    return await _fetch_paginated(_page, limit=limit_per_page, max_items=max_items)


@_cached()
async def find_client(mac_or_ip: str) -> dict | None:
    from mcp_servers.monitoring import find_client as _find
    return await _run(_find, mac_or_ip=mac_or_ip)


# ── Alerts ────────────────────────────────────────────────────────────────────

@_cached()
async def get_alerts(site_id: str | None = None, limit: int = 50) -> list[dict]:
    from mcp_servers.monitoring import list_alerts
    kwargs: dict[str, Any] = {"limit": limit}
    if site_id:
        kwargs["site_id"] = site_id
    return _unwrap(await _run(list_alerts, **kwargs))


# ── Device Groups & Sites (Classic Central) ──────────────────────────────────

@_cached()
async def get_device_groups() -> list[dict]:
    """List groups via Classic Central API."""
    def _fetch():
        c = get_classic_client()
        r = c.get("/configuration/v2/groups", params={"limit": 50, "offset": 0})
        r.raise_for_status()
        data = r.json()
        # Classic returns {"data": [["group1"],["group2"]], "total": N}
        names = []
        for row in data.get("data", []) if isinstance(data, dict) else []:
            if isinstance(row, (list, tuple)) and row:
                names.append(row[0])
            elif isinstance(row, str) and row:
                names.append(row)
        return [{"groupName": n} for n in names]
    return await _run(_fetch)


@_cached()
async def get_classic_sites() -> list[dict]:
    """List sites via Classic Central API — returns site_id (int), site_name, etc."""
    def _fetch():
        c = get_classic_client()
        r = c.get("/central/v2/sites", params={"limit": 100, "offset": 0})
        r.raise_for_status()
        data = r.json()
        sites = data.get("sites", []) if isinstance(data, dict) else []
        return sites if isinstance(sites, list) else []
    return await _run(_fetch)


@_cached()
async def get_display_sites() -> list[dict]:
    """Sites for display and filtering, from whichever API is answering.

    The site pickers (topology filter, alert-rule dropdown, command palette)
    all read the Classic Central gateway, which currently 401s on this tenant —
    so every one of them silently rendered an empty list. New Central's
    /network-config/v1/sites works and describes the same sites, so prefer it
    and keep Classic as the fallback. Callers should resolve names through
    aruba_central.site_display_name(), since the two payloads spell the name
    differently (scopeName vs site_name).
    """
    try:
        sites = await get_sites()
        if sites:
            return sites
    except Exception as exc:
        logger.debug("New Central sites unavailable: %s", exc)
    try:
        return await get_central_sites()
    except Exception as exc:
        logger.warning("No site source available (Classic Central: %s)", exc)
        return []


@_cached()
async def get_central_sites() -> list[dict]:
    """Alias for get_classic_sites."""
    return await get_classic_sites()


async def move_device_to_group(group_name: str, serials: list[str]) -> dict:
    """Move devices to a group via Classic Central API."""
    def _move():
        c = get_classic_client()
        r = c.post("/configuration/v1/devices/move", payload={"group": group_name, "serials": serials})
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"status_code": r.status_code, "response": body}
    return await _run(_move)


async def assign_device_to_site(site_id: int, serials: list[str], device_type: str = "IAP") -> dict:
    """Associate devices with a site via Classic Central API."""
    dtype_map = {"access_point": "IAP", "gateway": "CONTROLLER", "switch": "CX"}
    dt = dtype_map.get(device_type.lower(), device_type.upper()) if device_type else "IAP"
    def _assign():
        c = get_classic_client()
        r = c.post("/central/v2/sites/associations", payload={
            "site_id": int(site_id),
            "device_ids": serials,
            "device_type": dt,
        })
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"status_code": r.status_code, "response": body}
    return await _run(_assign)


# Keep old aliases that used centralmcp for backward compat
async def add_device_to_group(scope_id: str, serial_numbers: list[str]) -> dict:
    """Deprecated — use move_device_to_group instead."""
    return await move_device_to_group(scope_id, serial_numbers)


# ── Ops ───────────────────────────────────────────────────────────────────────

async def run_show(serial: str, device_type: str, commands: list[str]) -> dict:
    """Run show commands — picks the right function by device type."""
    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "aps":
        # Supported since centralmcp v0.4.0 — this used to return a refusal,
        # which made the detail page's "View Config" button dead on every AP.
        from mcp_servers.ops import ap_show
        return await _run(ap_show, serial, commands)
    if dtype == "cx":
        from mcp_servers.ops import cx_show
        return await _run(cx_show, serial, commands)
    if dtype == "aos-s":
        from mcp_servers.ops import aos_s_show
        return await _run(aos_s_show, serial, commands)
    if dtype == "gateways":
        from mcp_servers.ops import gateway_show
        return await _run(gateway_show, serial, commands)
    return _ops_error(f"Could not determine device type for show commands on {serial}.")


async def run_ping(serial: str, device_type: str, destination: str, count: int = 5) -> dict:
    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "aps":
        # v0.4.0 ships ap_ping; the old refusal broke the button on all 9 APs.
        from mcp_servers.ops import ap_ping
        return await _run(ap_ping, serial, destination, count=count)
    if dtype == "cx":
        from mcp_servers.ops import cx_ping
        return await _run(cx_ping, serial, destination, count=count)
    if dtype == "aos-s":
        from mcp_servers.ops import aos_s_ping
        return await _run(aos_s_ping, serial, destination)
    if dtype == "gateways":
        # Gateways have no dedicated ping tool; route through the gateway CLI.
        from mcp_servers.ops import gateway_show
        return await _run(gateway_show, serial, [f"ping {destination}"])
    return _ops_error(f"Could not determine device type for ping on {serial}.")


async def run_reboot(serial: str, device_type: str) -> dict:
    """Reboot a device via Central troubleshooting API (bypasses MCP elicitation)."""
    from mcp_servers.shared import _AOS_S_BASE, compact_http_error, get_client

    dtype = _resolve_troubleshoot_type(serial, device_type)
    if dtype == "aps":
        endpoint = f"/network-troubleshooting/v1alpha1/aps/{serial}/reboot"
        reboot_type = "AP"
    elif dtype == "cx":
        endpoint = f"/network-troubleshooting/v1alpha1/cx/{serial}/reboot"
        reboot_type = "CX"
    elif dtype == "aos-s":
        endpoint = f"{_AOS_S_BASE}/{serial}/reboot"
        reboot_type = "AOS-S"
    elif dtype == "gateways":
        endpoint = f"/network-troubleshooting/v1alpha1/gateways/{serial}/reboot"
        reboot_type = "GATEWAY"
    else:
        return {
            "status": "failed",
            "serial_number": serial,
            "device_type": device_type,
            "response": None,
            "errors": [f"Could not determine device type for reboot on {serial}."],
        }

    errors: list[str] = []
    client = get_client()
    try:
        response = await client._arequest("POST", endpoint, json={})
        if response.status_code not in (200, 201, 202):
            errors.append(compact_http_error(response))
            return {
                "status": "failed",
                "serial_number": serial,
                "device_type": reboot_type,
                "response": None,
                "errors": errors,
            }
        try:
            resp_body = response.json()
        except Exception:
            resp_body = {}
        return {
            "status": "submitted",
            "serial_number": serial,
            "device_type": reboot_type,
            "response": resp_body,
            "errors": errors,
        }
    except Exception as exc:
        errors.append(str(exc))
        return {
            "status": "failed",
            "serial_number": serial,
            "device_type": reboot_type,
            "response": None,
            "errors": errors,
        }


# ── GreenLake Platform (GLP) ──────────────────────────────────────────────────

@_cached()
async def get_glp_devices(limit: int = 200) -> list[dict]:
    from mcp_servers.glp import list_glp_devices
    result = await _run(list_glp_devices, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


@_cached()
async def get_glp_subscriptions(limit: int = 200) -> list[dict]:
    from mcp_servers.glp import list_glp_subscriptions
    result = await _run(list_glp_subscriptions, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


@_cached()
async def get_glp_users(limit: int = 300) -> list[dict]:
    from mcp_servers.glp import list_glp_users
    result = await _run(list_glp_users, limit=limit)
    return result.get("items", []) if isinstance(result, dict) else []


@_cached()
async def get_glp_audit_logs(limit: int = 100, category: str | None = None) -> list[dict]:
    from mcp_servers.glp import list_glp_audit_logs
    kwargs: dict[str, Any] = {"limit": limit}
    if category:
        kwargs["category"] = category
    result = await _run(list_glp_audit_logs, **kwargs)
    return result.get("items", []) if isinstance(result, dict) else []


async def assign_glp_subscription(serial_number: str, subscription_id: str) -> dict:
    """Assign a subscription UUID to a device via GLPClient.

    Sends the PATCH and returns immediately once accepted (202).
    Does NOT poll the async-operation — the page reload will show the result.
    """
    from mcp_servers.glp import get_glp_client

    def _do_assign():
        glp = get_glp_client()
        device_id = glp.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(f"Could not resolve serial {serial_number!r} to a GLP device ID.")
        body = {"subscription": [{"id": subscription_id}]}
        resp = glp._client._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"GLP PATCH returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "accepted", "http": resp.status_code}

    return await _run(_do_assign)


async def unassign_glp_subscription(serial_number: str) -> dict:
    """Remove all subscriptions from a device.

    Sends the PATCH and returns immediately once accepted (202).
    """
    from mcp_servers.glp import get_glp_client

    def _do_unassign():
        glp = get_glp_client()
        device_id = glp.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(f"Could not resolve serial {serial_number!r} to a GLP device ID.")
        body = {"subscription": []}
        resp = glp._client._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"GLP PATCH returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "accepted", "http": resp.status_code}

    return await _run(_do_unassign)


def _normalize_mac(mac: str) -> str:
    """Normalize a MAC address to colon-separated uppercase (AA:BB:CC:DD:EE:FF)."""
    import re
    raw = re.sub(r'[^0-9a-fA-F]', '', mac)
    if len(raw) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    upper = raw.upper()
    return ':'.join(upper[i:i+2] for i in range(0, 12, 2))


async def add_glp_device(serial_number: str, mac_address: str) -> dict:
    """Add a single device to the GLP workspace.

    Uses post_async and returns immediately with the task ID.
    """
    from mcp_servers.shared import get_glp_client
    mac = _normalize_mac(mac_address)

    def _do_add():
        glp = get_glp_client()
        body = {"network": [{"serialNumber": serial_number, "macAddress": mac}], "compute": [], "storage": []}
        location = glp._client.post_async("/devices/v1/devices", data=body)
        task_id = location.rstrip("/").split("/")[-1]
        return {"status": "accepted", "task_id": task_id}

    return await _run(_do_add)


async def add_glp_devices_bulk(devices: list[dict]) -> dict:
    """Add multiple devices to the GLP workspace.

    devices: list of {"serialNumber": "...", "macAddress": "..."}
    Returns immediately with the task ID.
    """
    from mcp_servers.shared import get_glp_client
    normalized = []
    for d in devices:
        serial = (d.get("serialNumber") or "").strip()
        mac = (d.get("macAddress") or "").strip()
        if not serial or not mac:
            raise ValueError(
                f"Each device needs serialNumber and macAddress; got: {d!r}"
            )
        normalized.append({"serialNumber": serial, "macAddress": _normalize_mac(mac)})

    def _do_bulk():
        glp = get_glp_client()
        body = {"network": normalized, "compute": [], "storage": []}
        location = glp._client.post_async("/devices/v1/devices", data=body)
        task_id = location.rstrip("/").split("/")[-1]
        return {"status": "accepted", "task_id": task_id, "count": len(normalized)}

    return await _run(_do_bulk)


async def assign_glp_device_to_app(serial_number: str) -> dict:
    """Assign a device to the Aruba Central application in GLP.

    Auto-detects the application ID and region from an existing assigned device.
    Must be called AFTER the device is added to GLP.
    """
    from mcp_servers.shared import get_glp_client

    def _do_assign_app():
        glp = get_glp_client()
        # Find app ID and region from an already-assigned device
        devs = glp.list_devices(limit=50)
        app_id = None
        region = None
        for d in devs:
            if d.get("application") and d.get("region"):
                app_id = d["application"]["id"]
                region = d["region"]
                break
        if not app_id:
            raise RuntimeError("Could not find an existing device with an application assignment to copy from.")

        device_id = glp.resolve_device_id(serial_number)
        if device_id is None:
            raise RuntimeError(f"Could not resolve serial {serial_number!r} to a GLP device ID.")

        body = {"application": {"id": app_id}, "region": region}
        resp = glp._client._request(
            "PATCH", "/devices/v2beta1/devices",
            params={"id": device_id}, json=body,
            headers={"Content-Type": "application/merge-patch+json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"GLP app assign PATCH returned HTTP {resp.status_code}: {resp.text[:300]}")
        return {"status": "accepted", "http": resp.status_code, "app_id": app_id, "region": region}

    return await _run(_do_assign_app)


# ── RAG Doc Search ────────────────────────────────────────────────────────────

async def search_docs(query: str, top_k: int = 5) -> list[dict]:
    """Hybrid doc search via centralmcp RAG (LanceDB by default, Redis optional)."""
    try:
        from mcp_servers.rag import search_docs as _search
        return await _run(_search, query, top_k=top_k)
    except Exception as exc:
        logger.warning("Doc search failed: %s", exc)
        return [{"error": str(exc)}]


async def lookup_api(query: str, top_k: int = 10) -> list[dict]:
    """Exact OpenAPI field/endpoint lookup via centralmcp specs index."""
    try:
        from mcp_servers.rag import lookup_api as _lookup
        return await _run(_lookup, query, top_k=top_k)
    except Exception as exc:
        logger.warning("API lookup failed: %s", exc)
        return [{"error": str(exc)}]


async def ask_docs(question: str, top_k: int = 3, source: str | None = None) -> dict:
    """Compact cited answer from local docs/API indexes."""
    try:
        from mcp_servers.rag import ask_docs as _ask
        result = await _run(_ask, question, top_k=top_k, source=source)
        return result if isinstance(result, dict) else {"answer": str(result), "citations": [], "mode": "unknown"}
    except Exception as exc:
        logger.warning("ask_docs failed: %s", exc)
        return {"answer": str(exc), "citations": [], "mode": "error"}


# ── MCP Tool Registry (Lab tester) ────────────────────────────────────────────

TOOL_REGISTRY: dict[str, list[dict]] = {
    "monitoring": [
        {"name": "list_sites",   "desc": "List all sites",                "params": '{"limit": 100}'},
        {"name": "list_devices", "desc": "List APs and switches",         "params": '{"limit": 50}'},
        {"name": "list_clients", "desc": "List connected clients",        "params": '{"limit": 50}'},
        {"name": "find_device",  "desc": "Look up a device by serial",    "params": '{"serial_number": "XXXXX"}'},
        {"name": "find_client",  "desc": "Look up a client by MAC or IP", "params": '{"mac_or_ip": "10.0.0.1"}'},
        {"name": "list_alerts",  "desc": "List active alerts",            "params": '{"limit": 20}'},
        {"name": "list_events",  "desc": "Events for a device (24h)",     "params": '{"serial_number": "XXXXX", "hours": 24}'},
    ],
    "ops": [
        {"name": "cx_ping",       "desc": "Ping from a CX switch",       "params": '{"serial_number": "XXXXX", "destination": "8.8.8.8"}'},
        {"name": "cx_traceroute", "desc": "Traceroute from a CX switch", "params": '{"serial_number": "XXXXX", "destination": "8.8.8.8"}'},
    ],
    "nac": [
        {"name": "list_mac_registrations", "desc": "List NAC MAC registrations", "params": '{"limit": 50}'},
    ],
    "docs": [
        {"name": "search_docs", "desc": "Hybrid search over Aruba docs", "params": '{"query": "how to configure VLAN", "top_k": 5}'},
        {"name": "lookup_api",  "desc": "Exact API schema/enum lookup",  "params": '{"query": "wlan ssid auth-type enum", "top_k": 10}'},
        {"name": "ask_docs",    "desc": "Compact cited doc answer",      "params": '{"question": "How do I assign a device to a site?", "top_k": 3}'},
    ],
}

# Flat map of tool name → (module, function) for dispatch
_TOOL_MAP: dict[str, tuple[str, str]] = {
    "list_sites":              ("mcp_servers.monitoring", "list_sites"),
    "list_devices":            ("mcp_servers.monitoring", "list_devices"),
    "list_clients":            ("mcp_servers.monitoring", "list_clients"),
    "find_device":             ("mcp_servers.monitoring", "find_device"),
    "find_client":             ("mcp_servers.monitoring", "find_client"),
    "list_alerts":             ("mcp_servers.monitoring", "list_alerts"),
    "list_events":             ("mcp_servers.monitoring", "list_events"),
    "cx_ping":                 ("mcp_servers.ops", "cx_ping"),
    "cx_traceroute":           ("mcp_servers.ops", "cx_traceroute"),
    "list_mac_registrations":  ("mcp_servers.nac", "list_mac_registrations"),
    "search_docs":             ("mcp_servers.rag", "search_docs"),
    "lookup_api":              ("mcp_servers.rag", "lookup_api"),
    "ask_docs":                ("mcp_servers.rag", "ask_docs"),
}


async def run_tool(tool_name: str, params_json: str) -> dict:
    """Execute a named centralmcp tool and return a result dict."""
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except json.JSONDecodeError as exc:
        return {
            "tool": tool_name,
            "params": {},
            "output": None,
            "status": "error",
            "error": f"Invalid JSON params: {exc}",
        }

    if tool_name not in _TOOL_MAP:
        output = await invoke_tool_router(tool_name, params)
        if "error" not in output:
            return {"tool": tool_name, "params": params, "output": output, "status": "success", "error": None}
        return {
            "tool": tool_name,
            "params": params,
            "output": None,
            "status": "error",
            "error": output.get("error") or f"Unknown tool: {tool_name}",
        }

    module_path, fn_name = _TOOL_MAP[tool_name]

    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name)
        output = await _run(fn, **params)
        return {"tool": tool_name, "params": params, "output": output, "status": "success", "error": None}
    except Exception as exc:
        logger.warning("Tool %s failed with params %s: %s", tool_name, params, exc)
        return {"tool": tool_name, "params": params, "output": None, "status": "error", "error": str(exc)}
