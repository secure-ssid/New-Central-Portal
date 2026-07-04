"""RF neighbor normalization tests."""

from routes.devices import _rf_neighbor_rows


def test_rf_neighbor_rows_from_list():
    raw = [{
        "apName": "near-ap",
        "serialNumber": "AP2SERIAL",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "band": "5GHz",
        "channel": 36,
        "rssi": -65,
    }]
    rows = _rf_neighbor_rows(raw)
    assert len(rows) == 1
    assert rows[0]["serial"] == "AP2SERIAL"
    assert rows[0]["channel"] == "36"
