"""Tests for unified alerts hub."""
from datetime import datetime, timezone

from routes.alerts import _normalize_central_alert, _time_ago


def test_time_ago_recent():
    now = datetime.now(timezone.utc)
    assert _time_ago(now.isoformat()) == "just now"


def test_normalize_central_alert_fields():
    alert = _normalize_central_alert({
        "alertName": "AP Down",
        "severity": "critical",
        "serialNumber": "AP1SERIAL",
        "deviceName": "lobby-ap",
        "timeAt": "2026-07-04T10:00:00Z",
    })
    assert alert["title"] == "AP Down"
    assert alert["severity"] == "critical"
    assert alert["device_serial"] == "AP1SERIAL"
    assert alert["time_ago"]


def test_alerts_hub_renders(client, mock_central, stub_db):
    r = client.get("/alerts/")
    assert r.status_code == 200
    assert "Alerts" in r.text
