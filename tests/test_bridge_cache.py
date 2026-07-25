"""Tests for central_bridge's short-TTL response cache.

The cache exists because every page route funnels through central_bridge with
no caching of its own, which generated enough upstream traffic to trip Central's
rate limiter — and centralmcp answers a 429 with a blocking sleep.
"""
import asyncio

import pytest

from vendors import central_bridge


@pytest.fixture(autouse=True)
def _clean():
    central_bridge.clear_bridge_cache()
    yield
    central_bridge.clear_bridge_cache()


def test_repeat_calls_inside_ttl_hit_the_cache():
    calls = []

    @central_bridge._cached()
    async def fetch(serial):
        calls.append(serial)
        return {"serial": serial}

    async def run():
        return [await fetch("ABC"), await fetch("ABC"), await fetch("ABC")]

    results = asyncio.run(run())
    assert results == [{"serial": "ABC"}] * 3
    assert calls == ["ABC"], "expected one upstream call, got %r" % (calls,)


def test_distinct_arguments_are_cached_separately():
    calls = []

    @central_bridge._cached()
    async def fetch(serial, hours=24):
        calls.append((serial, hours))
        return serial

    async def run():
        await fetch("A")
        await fetch("B")
        await fetch("A", hours=48)
        await fetch("A")

    asyncio.run(run())
    assert calls == [("A", 24), ("B", 24), ("A", 48)]


def test_expired_entries_are_served_stale_then_revalidated():
    """Stale-while-revalidate: nobody waits for a refresh they didn't need.

    Before this, every request that landed just after the TTL lapsed paid the
    full cold cost, so casual browsing kept hitting multi-second page loads.
    """
    calls = []

    @central_bridge._cached(ttl=0.01)
    async def fetch():
        calls.append(1)
        return {"n": len(calls)}

    async def run():
        first = await fetch()
        await asyncio.sleep(0.05)          # entry is now stale, not expired
        second = await fetch()             # served instantly from the stale copy
        await asyncio.sleep(0.05)          # let the background refresh land
        third = await fetch()
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first == {"n": 1}
    assert second == {"n": 1}, "stale value should be returned immediately"
    assert third == {"n": 2}, "background refresh should have updated the entry"
    # With a 10ms TTL the third read is itself stale and kicks off one more
    # refresh; what matters is that no caller ever blocked on a revalidation.
    assert 2 <= len(calls) <= 3


def test_stale_serving_stops_after_the_grace_window():
    calls = []

    @central_bridge._cached(ttl=0.01)
    async def fetch():
        calls.append(1)
        return {"n": len(calls)}

    async def run():
        await fetch()
        # Past fresh AND past stale — this caller must block on a real fetch.
        entry_key = ("fetch", (), ())
        _fresh, _stale, value = central_bridge._response_cache[entry_key]
        central_bridge._response_cache[entry_key] = (0.0, 0.0, value)
        return await fetch()

    assert asyncio.run(run()) == {"n": 2}
    assert len(calls) == 2


def test_empty_results_are_only_briefly_cached_and_never_served_stale():
    """centralmcp turns upstream failures into empty values instead of raising.

    MCPClient.get_devices_page catches Exception and returns ([], None), so a
    429 reaches the cache looking exactly like "this tenant has 0 devices".
    Caching that for the full TTL blanked the whole portal off one transient
    failure — precisely the condition this cache exists to handle.
    """
    calls = []

    @central_bridge._cached()
    async def fetch():
        calls.append(1)
        return [] if len(calls) == 1 else ["dev-1"]

    async def run():
        outage = await fetch()
        await asyncio.sleep(0.02)
        key = ("fetch", (), ())
        fresh_until, stale_until, _v = central_bridge._response_cache[key]
        # Short fresh window so recovery is picked up almost immediately...
        assert fresh_until - stale_until < 0
        assert fresh_until < central_bridge.time.monotonic() + 30
        # ...but it keeps the stale grace, or every genuinely-empty result
        # (no anomalies, quiet AP) would block on a live fetch.
        assert stale_until > fresh_until
        # Expire the fresh window the way the clock would seconds later.
        central_bridge._response_cache[key] = (0.0, stale_until, [])
        stale = await fetch()          # served instantly from the stale copy
        await asyncio.sleep(0.05)      # background refresh lands
        return outage, stale, await fetch()

    outage, stale, recovered = asyncio.run(run())
    assert outage == []
    assert stale == [], "must not block; the stale empty is served immediately"
    assert recovered == ["dev-1"], "the portal must recover as soon as Central does"


def test_error_envelopes_are_treated_as_low_confidence():
    """centralmcp returns {"errors": [...]} rather than raising."""

    @central_bridge._cached()
    async def fetch():
        return {"health": None, "errors": ["config-health: HTTP 429"]}

    asyncio.run(fetch())
    fresh_until, stale_until, _v = central_bridge._response_cache[("fetch", (), ())]
    # Short fresh window (seconds, not the full TTL) so recovery is fast.
    assert fresh_until < central_bridge.time.monotonic() + 30
    assert stale_until > fresh_until


def test_concurrent_callers_share_one_upstream_request():
    """The dashboard fired five byte-identical requests in the same millisecond."""
    calls = []

    @central_bridge._cached()
    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.05)  # window in which the other callers pile up
        return "payload"

    async def run():
        return await asyncio.gather(*(fetch() for _ in range(5)))

    results = asyncio.run(run())
    assert results == ["payload"] * 5
    assert len(calls) == 1, "expected coalescing to one call, got %d" % len(calls)


def test_failures_are_not_cached_and_propagate_to_all_waiters():
    calls = []

    @central_bridge._cached()
    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.02)
        raise RuntimeError("upstream down")

    async def run():
        results = await asyncio.gather(
            *(fetch() for _ in range(3)), return_exceptions=True
        )
        # A later call must retry rather than replay a cached failure.
        retry = await asyncio.gather(fetch(), return_exceptions=True)
        return results + retry

    outcomes = asyncio.run(run())
    assert all(isinstance(o, RuntimeError) for o in outcomes)
    assert len(calls) == 2, "one coalesced attempt, then one retry"


def test_fresh_data_bypasses_the_cache():
    """The alerting sweep must not inherit a browser tab's cached fleet."""
    calls = []

    @central_bridge._cached()
    async def fetch():
        calls.append(1)
        return len(calls)

    async def run():
        await fetch()
        await fetch()
        with central_bridge.fresh_data():
            bypassed = await fetch()
        return bypassed

    bypassed = asyncio.run(run())
    assert calls == [1, 1]
    assert bypassed == 2


def test_fresh_data_propagates_through_nested_calls():
    """get_all_devices -> _fetch_paginated -> get_devices must all bypass."""
    calls = []

    @central_bridge._cached()
    async def inner():
        calls.append("inner")
        return "v"

    @central_bridge._cached()
    async def outer():
        return await inner()

    async def run():
        await outer()
        with central_bridge.fresh_data():
            await outer()

    asyncio.run(run())
    assert calls == ["inner", "inner"]


def test_unhashable_arguments_bypass_the_cache_instead_of_raising():
    calls = []

    @central_bridge._cached()
    async def fetch(payload):
        calls.append(1)
        return "ok"

    async def run():
        return [await fetch({"a": 1}), await fetch({"a": 1})]

    assert asyncio.run(run()) == ["ok", "ok"]
    assert len(calls) == 2


def test_inflight_futures_are_not_shared_across_event_loops():
    """The alerting scheduler runs its own short-lived loop per sweep."""
    calls = []

    @central_bridge._cached(ttl=0.0)  # never serve from the TTL cache
    async def fetch():
        calls.append(1)
        return "v"

    # Two separate loops, as notifications._fetch_devices_sync does.
    assert asyncio.run(fetch()) == "v"
    assert asyncio.run(fetch()) == "v"
    assert len(calls) == 2


def test_clear_bridge_cache_drops_entries():
    calls = []

    @central_bridge._cached()
    async def fetch():
        calls.append(1)
        return len(calls)

    assert asyncio.run(fetch()) == 1
    assert asyncio.run(fetch()) == 1
    central_bridge.clear_bridge_cache()
    assert asyncio.run(fetch()) == 2


def test_read_only_fetchers_are_decorated():
    """Regression guard: these are the calls that generated the 429 storm."""
    for name in (
        "get_sites", "get_devices", "get_all_devices", "get_device",
        "get_clients", "get_all_clients", "get_alerts", "list_active_alerts",
        "list_wlans", "get_device_health", "get_device_events",
        "get_tenant_health", "get_site_health_summary", "get_device_groups",
    ):
        fn = getattr(central_bridge, name)
        assert getattr(fn, "__wrapped__", None) is not None, f"{name} is not cached"


def test_mutating_operations_are_not_cached():
    """Reboots, traceroutes and config reads must always hit the device."""
    for name in (
        "run_traceroute", "get_device_running_config", "invoke_tool_router",
    ):
        fn = getattr(central_bridge, name)
        assert getattr(fn, "__wrapped__", None) is None, f"{name} must not be cached"
