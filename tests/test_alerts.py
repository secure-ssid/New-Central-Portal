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
    assert 'id="alerts-live"' in r.text


def test_alerts_partial_fragment(client, mock_central, stub_db):
    r = client.get("/alerts/?partial=1")
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "Central Active Alerts" in r.text


def test_alerts_severity_filter(client, mock_central, stub_db):
    r = client.get("/alerts/?severity=critical")
    assert r.status_code == 200
    assert "severity=critical" in r.text or "Critical" in r.text


def test_alerts_search_filter(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def alerts(limit=100):
        return [
            {"alertName": "AP Down", "severity": "critical", "deviceName": "lobby-ap"},
            {"alertName": "High CPU", "severity": "major", "deviceName": "core-sw"},
        ]

    monkeypatch.setattr(cb, "list_active_alerts", alerts)
    r = client.get("/alerts/?q=cpu")
    assert r.status_code == 200
    assert "High CPU" in r.text
    assert "AP Down" not in r.text


def test_alerts_pagination(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def alerts(limit=100):
        return [
            {"alertName": f"Alert {i}", "severity": "minor", "deviceName": f"dev-{i}"}
            for i in range(5)
        ]

    monkeypatch.setattr(cb, "list_active_alerts", alerts)
    r = client.get("/alerts/?per_page=2")
    assert r.status_code == 200
    assert "Page 1 of 3" in r.text
    assert "Rows" in r.text


def test_alerts_search_form_outside_live_region(client, mock_central, stub_db):
    html = client.get("/alerts/").text
    assert html.index('aria-label="Search alerts"') < html.index('id="alerts-live"')
    partial = client.get("/alerts/?partial=1").text
    assert 'aria-label="Search alerts"' not in partial
