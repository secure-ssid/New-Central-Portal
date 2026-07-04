"""Tests for AP wireless card normalization."""
from routes.devices import _wireless_cards


def test_wireless_cards_from_radios():
    cards = _wireless_cards(
        {"clientCount": 12, "noiseFloor": -95},
        {"radios": [{"band": "5GHz", "channel": 36, "txPower": 18, "numClients": 8}]},
        {"utilization": 42},
    )
    assert cards["channel_util_pct"] == "42"
    assert len(cards["radios"]) == 1
    assert cards["radios"][0]["band"] == "5GHz"
    assert any(m["label"] == "clientCount" for m in cards["metrics"])


def test_wireless_cards_empty():
    cards = _wireless_cards(None, None, None)
    assert cards == {"radios": [], "metrics": [], "channel_util_pct": None}
