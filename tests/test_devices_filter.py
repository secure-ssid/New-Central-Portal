"""Device list status filter tests."""


def test_devices_status_offline_filter(client, mock_central, stub_db):
    r = client.get("/devices/?status=offline")
    assert r.status_code == 200
    # Mock fleet has GW1SERIAL and SW3SERIAL offline (2 devices)
    assert "Showing 2 offline" in r.text


def test_devices_status_online_filter(client, mock_central, stub_db):
    r = client.get("/devices/?status=online")
    assert r.status_code == 200
    assert "Showing 3 online" in r.text


def test_devices_site_filter(client, mock_central, stub_db):
    r = client.get("/devices/?site=HQ")
    assert r.status_code == 200
    assert "Showing 3 devices at HQ" in r.text
    assert "branch-gw-1" not in r.text


def test_devices_type_switch_filter(client, mock_central, stub_db):
    r = client.get("/devices/?type=switch")
    assert r.status_code == 200
    assert "3 switch" in r.text
    assert "lobby-ap-1" not in r.text
