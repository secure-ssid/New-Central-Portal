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
    cards = _tenant_health_cards({"status": "healthy", "apiLatency": 42, "items": []})
    assert cards[0]["label"] == "status"
    assert cards[0]["tone"] == "ok"
    assert any(c["value"] == "42" for c in cards)


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
