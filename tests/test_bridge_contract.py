"""Contract tests for the raw/normalized boundary between the bridge and the app.

These exist because a whole class of defect hid behind 446 green tests: the
notification engine reads normalized keys (serial/name/site/type) while
``central_bridge.get_devices()`` returns RAW centralmcp keys
(serialNumber/deviceName/siteName/deviceType). Every other fixture in the suite
stubs ``central_bridge`` itself, so nothing pinned that boundary.

The rule these tests enforce: the bridge returns raw payloads and does NOT
normalize; ``_norm_device`` is the single normalizer and it must cover every key
the engine reads. Stub one layer BELOW the code under test — here that means
faking ``mcp_servers.*`` via ``sys.modules`` rather than patching the bridge.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from vendors import central_bridge as cb  # noqa: E402
from vendors.aruba_central import _norm_device  # noqa: E402

# Live-tenant shape (verified against Central): uppercase status, camelCase keys.
RAW_DEVICE = {
    "serialNumber": "SAMPLE0002",
    "deviceName": "SAMPLE-SWITCH",
    "deviceType": "SWITCH",
    "status": "ONLINE",
    "siteName": "HQ",
    "model": "6300",
    "ipv4": "10.0.0.2",
    "macAddress": "aa:bb:cc:dd:ee:ff",
}


@pytest.fixture
def fake_monitoring(monkeypatch):
    """Inject a fake ``mcp_servers.monitoring`` so the real bridge code runs."""
    def list_devices(**kwargs):
        return {"items": [dict(RAW_DEVICE)], "total": 1, "count": 1, "next": None}

    monitoring = types.ModuleType("mcp_servers.monitoring")
    monitoring.list_devices = list_devices
    pkg = types.ModuleType("mcp_servers")
    pkg.monitoring = monitoring
    monkeypatch.setitem(sys.modules, "mcp_servers", pkg)
    monkeypatch.setitem(sys.modules, "mcp_servers.monitoring", monitoring)
    return monitoring


def test_get_devices_returns_raw_centralmcp_keys(fake_monitoring):
    """The bridge must pass payloads through unnormalized."""
    devices = asyncio.run(cb.get_devices(limit=1))
    assert devices, "bridge returned no devices"
    d = devices[0]
    assert {"serialNumber", "deviceName", "deviceType", "status"} <= set(d)
    # Tripwire: if someone "helpfully" normalizes inside the bridge, the
    # notification engine's contract silently changes underneath it.
    assert "serial" not in d


def test_norm_device_maps_every_key_the_engine_reads():
    """notifications.py reads serial/name/site/type/status — all must resolve."""
    d = _norm_device(dict(RAW_DEVICE))
    assert d["serial"] == "SAMPLE0002"
    assert d["name"] == "SAMPLE-SWITCH"
    assert d["site"] == "HQ"
    assert d["type"] == "switch"
    assert d["status"] == "online"


def test_norm_device_is_idempotent():
    """Licenses normalizing at the engine entry points: double-normalizing is safe."""
    once = _norm_device(dict(RAW_DEVICE))
    twice = _norm_device(dict(once))
    for key in ("serial", "name", "site", "type", "status"):
        assert twice[key] == once[key], f"{key} changed on re-normalization"


def test_norm_device_handles_uppercase_access_point():
    d = _norm_device({"serialNumber": "X", "deviceType": "ACCESS_POINT", "status": "ONLINE"})
    assert d["type"] == "access_point"


# ── Trend wrappers: the caching contract ─────────────────────────────────────
#
# These wrappers exist under a trap. _is_low_confidence() treats any dict with a
# truthy "errors" key as a possible upstream failure and cuts its TTL from 60s
# to 5s — and every monitoring envelope carries a non-empty errors[] on success,
# logging the 404s from candidates tried before the one that worked. A wrapper
# that returned the envelope would look correct, pass a naive cache test, and
# silently become the most refetched call in the portal.

TRENDS_ENVELOPE = {
    "serial_number": "SAMPLE0002",
    "endpoint_used": "/network-monitoring/v1/switches/SAMPLE0002/hardware-trends",
    "errors": ["404 at /network-monitoring/v1alpha1/switch/SAMPLE0002/hardware-trends"],
    "trends": {"response": {"metric": "SwitchDeviceTrends", "keys": ["cpuUtilization"],
        "switchMetrics": [{"serialNumber": "SAMPLE0002", "samples": [
            {"timestamp": 1784982600000, "data": ["21"]}]}]}},
}

NOT_FOUND_ENVELOPE = {
    "serial_number": "X", "trends": None, "endpoint_used": None,
    "errors": ["404 at /a", "404 at /b"],
}


class _Recorder:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_kwargs: dict = {}

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self.payload


def _install(monkeypatch, **fns):
    monitoring = types.ModuleType("mcp_servers.monitoring")
    for name, fn in fns.items():
        setattr(monitoring, name, fn)
    pkg = types.ModuleType("mcp_servers")
    pkg.monitoring = monitoring
    monkeypatch.setitem(sys.modules, "mcp_servers", pkg)
    monkeypatch.setitem(sys.modules, "mcp_servers.monitoring", monitoring)
    return monitoring


@pytest.fixture(autouse=True)
def _clear_cache():
    cb.clear_bridge_cache()
    yield
    cb.clear_bridge_cache()


def test_trend_wrapper_unwraps_the_envelope_so_it_keeps_the_full_ttl(monkeypatch):
    rec = _Recorder(TRENDS_ENVELOPE)
    _install(monkeypatch, get_device_trends=rec)

    out = asyncio.run(cb.get_switch_hardware_trends("SAMPLE0002", "S", "E"))

    assert "response" in out, "must return the inner payload, not the envelope"
    assert "errors" not in out
    assert cb._is_low_confidence(out) is False, (
        "returning the envelope would drop this to a 5s TTL because its "
        "errors[] is non-empty even on success")


def test_a_missing_payload_becomes_None(monkeypatch):
    _install(monkeypatch, get_device_trends=_Recorder(NOT_FOUND_ENVELOPE))
    out = asyncio.run(cb.get_ap_trends("X", "cpu", "S", "E"))
    assert out is None
    assert cb._is_low_confidence(out) is True, "a failure should be retried soon"


def test_switch_trends_are_fetched_once_not_once_per_metric(monkeypatch):
    """centralmcp maps cpu, memory and hardware to the same endpoint."""
    rec = _Recorder(TRENDS_ENVELOPE)
    _install(monkeypatch, get_device_trends=rec)

    async def run():
        return await asyncio.gather(
            cb.get_switch_hardware_trends("SAMPLE0002", "S", "E"),
            cb.get_switch_hardware_trends("SAMPLE0002", "S", "E"),
        )

    asyncio.run(run())
    assert rec.calls == 1


def test_ap_trends_always_pass_device_type(monkeypatch):
    """Omitting it makes centralmcp resolve the type with an extra inventory
    call, on every metric, on every request."""
    rec = _Recorder(TRENDS_ENVELOPE)
    _install(monkeypatch, get_device_trends=rec)
    asyncio.run(cb.get_ap_trends("PHQHKZ21HK", "cpu", "S", "E"))
    assert rec.last_kwargs.get("device_type") == "AP"


def test_poe_tolerates_a_null_items_list(monkeypatch):
    """`items` can be present and null — the get_switch_ports lesson."""
    _install(monkeypatch, get_switch_interface_poe=_Recorder(
        {"poe": {"response": {"count": 0, "items": None}}, "errors": []}))
    assert asyncio.run(cb.get_switch_interface_poe("SAMPLE0002")) == []


def test_vlans_accept_either_payload_shape(monkeypatch):
    """centralmcp returns data.get("vlans", data.get("items", data)), so the
    value may be a list or the whole dict."""
    _install(monkeypatch, get_switch_vlans=_Recorder(
        {"vlans": {"items": [{"id": "1"}]}, "errors": []}))
    assert asyncio.run(cb.get_switch_vlans("SAMPLE0002")) == [{"id": "1"}]


# Diagnostics that poll an async job: mcp_servers/shared.py sleeps 5s BEFORE
# its first poll, so each costs 5-60s while holding a thread-pool worker and an
# upstream semaphore slot. Caching them would be wrong (the user asked to run it
# now), which is exactly why they must never be auto-fired on page load.
DELIBERATELY_UNCACHED = {
    "get_switch_port_errors", "get_cx_mac_table", "find_mac_on_switch",
    "get_switch_spanning_tree", "get_cx_arp_table", "get_device_running_config",
    "get_classic_client", "get_glp_subscriptions_raw",
    # Thin filter over the cached list_gateway_stats — no upstream call of its own.
    "get_gateway_stats",
}


def test_every_read_wrapper_is_cached():
    """Mechanises the rule for wrappers that do not exist yet.

    An undecorated read path bypasses stale-while-revalidate and blocks the
    request on a cold upstream fetch — the defect that left the dashboard at 6s
    after every cache expiry.
    """
    import inspect

    offenders = []
    for name, fn in vars(cb).items():
        if not name.startswith(("get_", "list_", "find_")):
            continue
        if not inspect.iscoroutinefunction(fn) or name in DELIBERATELY_UNCACHED:
            continue
        if getattr(fn, "__wrapped__", None) is None:
            offenders.append(name)
    assert not offenders, (
        f"{offenders} bypass the response cache — add @_cached(), or add the "
        f"name to DELIBERATELY_UNCACHED with a reason")


# The upstream paths this app calls directly (the rest goes through centralmcp).
# Checked against Central on 2026-10-09: monitoring/config paths answer on the
# v1/v1alpha1 gateways, GreenLake device management is v2beta1, and the Classic
# client keeps its /central and /configuration v1/v2 paths.
PINNED_ENDPOINTS = (
    "/network-notifications/v1/alerts",
    "/network-services/v1alpha1/firmware-details",
    "/network-config/v1/named-vlan",
    "/network-troubleshooting/v1alpha1/aps/",
    "/network-troubleshooting/v1alpha1/cx/",
    "/network-troubleshooting/v1alpha1/gateways/",
    "/devices/v2beta1/devices",
    "/devices/v1/devices",
    "/configuration/v2/groups",
    "/central/v2/sites",
    "/configuration/v1/devices/move",
    "/central/v2/sites/associations",
)


def test_direct_upstream_endpoints_are_pinned():
    """A version segment changes without notice, so every path is listed here.

    Nothing else in the suite would notice a rename: the call sites answer from
    stubs, and a wrong version returns 404 at runtime, which the bridge turns
    into a quiet empty list. Changing a string in this list means re-verifying
    it against Central (or centralmcp), not editing the test.
    """
    src = Path(cb.__file__).read_text(encoding="utf-8")
    missing = [p for p in PINNED_ENDPOINTS if p not in src]
    assert not missing, (
        f"these Central/GreenLake paths are no longer called by the bridge: {missing}"
    )


def test_mock_fallback_fleet_is_placeholders():
    """The demo fleet is rendered in a public repo and shipped to every
    deployment without a Central token, so it may not carry real identifiers:
    no vendor-OUI MAC, no routable-looking address, no tenant site name.

    (Documentation/test IPs come from RFC 5737 TEST-NET-1, 192.0.2.0/24.)
    """
    from vendors.aruba_central import _mock_clients, _mock_devices

    offenders = []
    for d in _mock_devices():
        if not d["serial"].startswith("SAMPLE"):
            offenders.append(f"device serial {d['serial']!r}")
        if not d["mac"].startswith("02:00:00"):
            offenders.append(f"device mac {d['mac']!r}")
        if not d["ip"].startswith("192.0.2."):
            offenders.append(f"device ip {d['ip']!r}")
        if d["site"] != "Example HQ":
            offenders.append(f"device site {d['site']!r}")
    for c in _mock_clients():
        if not c["mac"].startswith("02:00:00"):
            offenders.append(f"client mac {c['mac']!r}")
        if not c["ip"].startswith("192.0.2."):
            offenders.append(f"client ip {c['ip']!r}")
    assert not offenders, (
        f"the mock fallback leaks identifying values — use SAMPLE*/02:00:00:*/"
        f"192.0.2.x: {offenders}")
