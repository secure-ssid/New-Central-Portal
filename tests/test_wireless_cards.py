"""Tests for AP wireless card normalization."""
from routes.devices import _wireless_cards


def test_wireless_cards_from_radios():
    # Real payload shapes: RF fields live on each radio (channelUtilization,
    # power, clientCount); AP summary metrics live under `metrics`; the
    # channel-util summary is aggregated from the radios, not a top-level key.
    cards = _wireless_cards(
        {"metrics": {"mode": "Client Access", "currentUplinkInUse": "Ethernet"}},
        {"radios": [{"band": "5GHz", "channel": "36", "power": "18",
                     "clientCount": 8, "channelUtilization": "42"}]},
        None,
    )
    assert cards["channel_util_pct"] == "42"
    assert len(cards["radios"]) == 1
    assert cards["radios"][0]["band"] == "5GHz"
    assert cards["radios"][0]["power"] == "18"
    assert any(m["label"] == "Mode" for m in cards["metrics"])


def test_wireless_cards_empty():
    cards = _wireless_cards(None, None, None)
    assert cards == {"radios": [], "metrics": [], "channel_util_pct": None}
