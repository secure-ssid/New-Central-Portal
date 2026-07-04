"""Unit tests for topology_graph edge builders."""
from topology_graph import (
    ap_serials_missing_edges,
    edges_from_ap_uplinks,
    edges_from_switch_ports,
)


SW1 = {"serial": "SW1", "type": "switch", "status": "online", "name": "core"}
SW2 = {"serial": "SW2", "type": "switch", "status": "online", "name": "edge"}
AP1 = {"serial": "AP1", "type": "access_point", "status": "online", "name": "lobby-ap"}


def test_edges_from_switch_ports_basic():
    ports = [
        {"name": "1/1/1", "neighbourSerial": "AP1", "neighbourType": "Access Point", "speed": 1000},
    ]
    edges, fails, seen = edges_from_switch_ports([SW1], [ports], {"SW1", "AP1"})
    assert fails == 0
    assert len(edges) == 1
    assert edges[0]["source"] == "SW1"
    assert edges[0]["target"] == "AP1"
    assert edges[0]["via"] == "ports"


def test_ap_uplink_supplement():
    devices = [SW1, AP1]
    edges = []
    seen = set()
    missing = ap_serials_missing_edges(devices, edges)
    assert len(missing) == 1
    uplinks = [{"switch_serial": "SW1", "port": "1/1/24"}]
    extra = edges_from_ap_uplinks(missing, uplinks, {"SW1", "AP1"}, seen)
    assert len(extra) == 1
    assert extra[0]["target"] == "AP1"
    assert extra[0]["via"] == "uplink"


def test_self_loop_and_client_skipped():
    ports = [
        {"name": "1/1/1", "neighbourSerial": "SW1", "neighbourType": "Switch"},
        {"name": "1/1/2", "neighbourSerial": "AA:BB:CC:DD:EE:FF", "neighbourType": "client"},
    ]
    edges, _, _ = edges_from_switch_ports([SW1], [ports], {"SW1"})
    assert edges == []
