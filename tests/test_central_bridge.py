"""Unit tests for central_bridge helpers (no live centralmcp required)."""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from vendors import central_bridge as cb


def test_run_awaits_coroutine_functions():
    async def _exercise():
        async def async_add(a, b):
            return a + b

        return await cb._run(async_add, 2, 3)

    assert asyncio.run(_exercise()) == 5


def test_run_executes_sync_functions():
    async def _exercise():
        def sync_mul(a, b):
            return a * b

        return await cb._run(sync_mul, 3, 4)

    assert asyncio.run(_exercise()) == 12


def test_unwrap_list_and_bounded_dict():
    assert cb._unwrap([{"a": 1}]) == [{"a": 1}]
    assert cb._unwrap({"items": [{"b": 2}], "_pagination": {}}) == [{"b": 2}]
    assert cb._unwrap(None) == []


def test_ops_error_shape():
    err = cb._ops_error("nope")
    assert err == {"status": None, "errors": ["nope"]}


def _fake_ops(monkeypatch, **tools):
    """Inject a fake mcp_servers.ops exposing only the named tools."""
    ops = types.ModuleType("mcp_servers.ops")
    for name, fn in tools.items():
        setattr(ops, name, fn)
    pkg = sys.modules.get("mcp_servers") or types.ModuleType("mcp_servers")
    pkg.ops = ops
    monkeypatch.setitem(sys.modules, "mcp_servers", pkg)
    monkeypatch.setitem(sys.modules, "mcp_servers.ops", ops)


# centralmcp v0.4.0 added ap_show/ap_ping/ap_traceroute. Until then the bridge
# returned a hardcoded refusal, which left "View Config" and "Ping from Device"
# permanently broken on every AP (9 of this fleet's 13 devices) — these tests
# previously asserted that refusal was correct.

def test_run_show_dispatches_to_ap_show(monkeypatch):
    monkeypatch.setattr(cb, "_resolve_troubleshoot_type", lambda serial, dtype: "aps")
    calls = []

    async def ap_show(serial, commands):
        calls.append((serial, commands))
        return {"status": "COMPLETED", "output": "AOS 10"}

    _fake_ops(monkeypatch, ap_show=ap_show)
    result = asyncio.run(cb.run_show("AP1", "access_point", ["show version"]))
    assert calls == [("AP1", ["show version"])]
    assert result["status"] == "COMPLETED"


def test_run_ping_dispatches_to_ap_ping(monkeypatch):
    monkeypatch.setattr(cb, "_resolve_troubleshoot_type", lambda serial, dtype: "aps")
    calls = []

    async def ap_ping(serial, destination, count=5):
        calls.append((serial, destination, count))
        return {"status": "COMPLETED"}

    _fake_ops(monkeypatch, ap_ping=ap_ping)
    assert asyncio.run(cb.run_ping("AP1", "access_point", "8.8.8.8"))["status"] == "COMPLETED"
    assert calls == [("AP1", "8.8.8.8", 5)]


def test_run_ping_on_gateway_uses_gateway_cli(monkeypatch):
    monkeypatch.setattr(cb, "_resolve_troubleshoot_type", lambda serial, dtype: "gateways")
    calls = []

    async def gateway_show(serial, commands):
        calls.append((serial, commands))
        return {"status": "COMPLETED"}

    _fake_ops(monkeypatch, gateway_show=gateway_show)
    asyncio.run(cb.run_ping("GW1", "gateway", "8.8.8.8"))
    assert calls == [("GW1", ["ping 8.8.8.8"])]


def test_run_traceroute_dispatches_to_ap_traceroute(monkeypatch):
    monkeypatch.setattr(cb, "_resolve_troubleshoot_type", lambda serial, dtype: "aps")
    calls = []

    async def ap_traceroute(serial, destination):
        calls.append((serial, destination))
        return {"status": "COMPLETED"}

    _fake_ops(monkeypatch, ap_traceroute=ap_traceroute)
    asyncio.run(cb.run_traceroute("AP1", "access_point", "8.8.8.8"))
    assert calls == [("AP1", "8.8.8.8")]


def test_unwrap_named_collection_key():
    """list_ssids returns {"wlan-ssid": [...]} — the WLAN page depended on this."""
    payload = {"wlan-ssid": [{"essid": "Air Pass"}], "_pagination": {}}
    assert cb._unwrap(payload, "wlan-ssid") == [{"essid": "Air Pass"}]


def test_unwrap_prefers_named_key_then_items():
    assert cb._unwrap({"items": [{"a": 1}], "other": [{"b": 2}]}, "other") == [{"b": 2}]
    assert cb._unwrap({"items": [{"a": 1}]}, "absent") == [{"a": 1}]


def test_unwrap_never_returns_an_error_envelope_as_data():
    """Most centralmcp tools carry an "errors" list; it must never become rows."""
    assert cb._unwrap({"status": None, "errors": ["boom"]}) == []
    assert cb._unwrap({"poe": None, "errors": ["404"]}, "poe") == []
