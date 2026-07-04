"""Tests for /api/status and probe_status mock detection."""
import asyncio

from routes.status import probe_status


def test_probe_status_live(mock_central, stub_db):
    result = asyncio.run(probe_status())
    assert result["central"] == "connected"
    assert result["data_mode"] == "live"
    assert result["mode"] == "live"
    assert result["severity"] == "ok"
    assert result["db"] == "ok"


def test_probe_status_mock_fallback(monkeypatch, stub_db):
    from vendors import aruba_central as ac

    async def mock_devices(**kwargs):
        ac._data_source = "mock"
        return [{"serial": "X", "name": "mock", "status": "online", "type": "switch", "site": "HQ"}]

    monkeypatch.setattr(ac.aruba, "get_devices", mock_devices)

    result = asyncio.run(probe_status())
    assert result["data_mode"] == "mock"
    assert result["mode"] == "mock"
    assert result["severity"] == "warn"


def test_api_status_endpoint(client, mock_central, stub_db):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "mode" in data
    assert "data_mode" in data
    assert data["central"] == "connected"
