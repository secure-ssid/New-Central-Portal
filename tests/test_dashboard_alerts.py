"""Dashboard alert fetch and ticker tests."""

import asyncio

from routes.home import _fetch_dashboard_alerts, _severity_class


def test_severity_class():
    assert _severity_class("CRITICAL") == "critical"
    assert _severity_class("high") == "major"


def test_fetch_dashboard_alerts_single_call(monkeypatch):
    calls = {"n": 0}

    async def fake_list(limit=50):
        calls["n"] += 1
        return [
            {"alertName": "AP offline", "severity": "critical", "serialNumber": "AP1"},
            {"alertName": "Minor", "severity": "minor"},
        ]

    monkeypatch.setattr(
        "vendors.central_bridge.list_active_alerts",
        fake_list,
    )

    summary, ticker = asyncio.run(_fetch_dashboard_alerts())
    assert calls["n"] == 1
    assert summary["total"] == 2
    assert summary["critical"] == 1
    assert len(ticker) == 1
    assert ticker[0]["severity"] == "critical"


def test_base_alpine_init_single_listener(client, mock_central, stub_db):
    """Alpine stores must register inside one alpine:init callback."""
    html = client.get("/").text
    start = html.find("document.addEventListener('alpine:init'")
    assert start >= 0
    script = html[start:html.find("</script>", start)]
    portal_idx = script.find("Alpine.store('portal'")
    ui_idx = script.find("Alpine.store('ui'")
    close_idx = script.rfind("});")
    assert portal_idx >= 0 and ui_idx >= 0
    assert portal_idx < ui_idx < close_idx


def test_alerts_partial_includes_tabs(client, mock_central, stub_db):
    r = client.get("/alerts/?partial=1")
    assert r.status_code == 200
    assert "Critical" in r.text
    assert "Central Active Alerts" in r.text
