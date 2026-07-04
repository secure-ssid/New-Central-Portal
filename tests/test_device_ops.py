"""Device ops panel HTMX endpoints (traceroute, LLDP, port errors, find MAC, MAC table)."""
import pytest


@pytest.fixture
def switch_client(client, mock_central):
    return client


def test_traceroute_invalid_dest(switch_client):
    r = switch_client.post(
        "/devices/SW1SERIAL/traceroute",
        data={"destination": "not a valid host!!!"},
    )
    assert r.status_code == 200
    assert "Invalid destination" in r.text


def test_find_mac_invalid(switch_client):
    r = switch_client.post(
        "/devices/SW1SERIAL/find-mac",
        data={"mac_address": "bad-mac"},
    )
    assert r.status_code == 200
    assert "Invalid MAC" in r.text


def test_lldp_returns_html(switch_client, monkeypatch):
    from vendors import central_bridge as cb

    async def fake_lldp(serial):
        return {"neighbors": [{"localPort": "1/1/1", "neighborName": "sw-2", "neighborPort": "1/1/2"}]}

    monkeypatch.setattr(cb, "get_lldp_neighbors", fake_lldp)
    r = switch_client.post("/devices/SW1SERIAL/lldp")
    assert r.status_code == 200
    assert "sw-2" in r.text


def test_port_errors_table(switch_client, monkeypatch):
    from vendors import central_bridge as cb

    async def fake_errors(serial, interface=None):
        return {"ports": [{"interface": "1/1/1", "errors": 12, "severity": "minor"}]}

    monkeypatch.setattr(cb, "get_switch_port_errors", fake_errors)
    r = switch_client.post("/devices/SW1SERIAL/port-errors", data={"interface": ""})
    assert r.status_code == 200
    assert "1/1/1" in r.text
    assert "12" in r.text


def test_mac_table_on_switch(switch_client, monkeypatch):
    from vendors import central_bridge as cb

    async def fake_table(serial, interface=None):
        return {"entries": [{"mac": "AA:BB:CC:DD:EE:FF", "vlan": 10, "port": "1/1/5", "type": "dynamic"}]}

    monkeypatch.setattr(cb, "get_cx_mac_table", fake_table)
    r = switch_client.post("/devices/SW1SERIAL/mac-table", data={"interface": ""})
    assert r.status_code == 200
    assert "AA:BB:CC:DD:EE:FF" in r.text


def test_mac_table_rejects_ap(switch_client):
    r = switch_client.post("/devices/AP1SERIAL/mac-table", data={"interface": ""})
    assert r.status_code == 200
    assert "only available on switches" in r.text
