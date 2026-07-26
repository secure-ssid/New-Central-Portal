"""Dashboard widget helpers."""
from routes.home import (
    _enrich_site_cards,
    _health_tone,
    _severity_class,
    _tenant_health_cards,
)


def test_health_tone_mapping():
    assert _health_tone("healthy") == "ok"
    assert _health_tone("degraded") == "warn"
    assert _health_tone("critical failure") == "critical"
    assert _health_tone(None) == "neutral"


def test_tenant_health_cards():
    # Real payload shape: health nested under device_health.deviceTypes[].
    cards = _tenant_health_cards({"device_health": {"deviceTypes": [
        {"name": "Access Points", "health": {"groups": [
            {"name": "Poor", "value": 0}, {"name": "Fair", "value": 0},
            {"name": "Good", "value": 6}]}}]}})
    assert cards[0]["label"] == "Access Points"
    assert cards[0]["tone"] == "ok"
    assert cards[0]["value"] == "all 6 good"


def test_enrich_site_cards_device_counts():
    cards = _enrich_site_cards(
        [{"id": 1, "name": "HQ", "label": "ok"}],
        [
            {"site": "HQ", "status": "online"},
            {"site": "HQ", "status": "online"},
            {"site": "HQ", "status": "offline"},
        ],
    )
    assert cards[0]["device_total"] == 3
    assert cards[0]["device_online"] == 2
    assert cards[0]["device_pct"] == 67


def test_severity_class():
    assert _severity_class("CRITICAL") == "critical"
    assert _severity_class("high") == "major"
