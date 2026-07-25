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
    "serialNumber": "SG30LMR164",
    "deviceName": "CX6300-CORE",
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
    assert d["serial"] == "SG30LMR164"
    assert d["name"] == "CX6300-CORE"
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
