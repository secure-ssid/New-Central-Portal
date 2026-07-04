"""Client list type filter tests."""


def test_clients_wireless_type_filter(client, mock_central, stub_db):
    r = client.get("/clients/?type=wireless")
    assert r.status_code == 200
    assert "1 wireless session" in r.text
    assert "laptop-1" in r.text
    assert "printer-1" not in r.text


def test_clients_wired_type_filter(client, mock_central, stub_db):
    r = client.get("/clients/?type=wired")
    assert r.status_code == 200
    assert "1 wired session" in r.text
    assert "printer-1" in r.text
    assert "laptop-1" not in r.text


def test_clients_type_tabs_show_fleet_totals(client, mock_central, stub_db):
    r = client.get("/clients/?type=wireless&per_page=1")
    assert r.status_code == 200
    # All-tab badge shows fleet total (2), not just the filtered page size (1).
    assert ">2<" in r.text
    assert "Wireless" in r.text
