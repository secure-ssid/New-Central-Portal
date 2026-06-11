"""Topology graph payload built from mocked devices + switch ports."""
import json
import re

import pytest

NODE_KEYS = {"id", "label", "group", "model", "status", "ip", "site", "url"}
EDGE_KEYS = {"id", "source", "target", "port", "speed"}


def extract_graph(html: str) -> dict:
    m = re.search(r"var RAW = (\{.*\});", html)
    assert m, "graph payload not found in topology page"
    # The route escapes '</' so the JSON can never close its <script> tag.
    return json.loads(m.group(1).replace("<\\/", "</"))


@pytest.fixture
def graph(client, mock_central):
    r = client.get("/topology/")
    assert r.status_code == 200
    return extract_graph(r.text)


class TestNodes:
    def test_every_device_with_serial_becomes_a_node(self, graph):
        ids = {n["id"] for n in graph["nodes"]}
        assert ids == {"SW1SERIAL", "SW2SERIAL", "AP1SERIAL",
                       "GW1SERIAL", "SW3SERIAL"}

    def test_node_field_contract(self, graph):
        for node in graph["nodes"]:
            assert set(node) == NODE_KEYS, node

    def test_group_mapping(self, graph):
        groups = {n["id"]: n["group"] for n in graph["nodes"]}
        assert groups["SW1SERIAL"] == "switch"
        assert groups["AP1SERIAL"] == "ap"
        assert groups["GW1SERIAL"] == "gateway"

    def test_offline_devices_kept_with_status(self, graph):
        by_id = {n["id"]: n for n in graph["nodes"]}
        assert by_id["GW1SERIAL"]["status"] == "offline"
        assert by_id["SW3SERIAL"]["status"] == "offline"
        assert by_id["SW1SERIAL"]["status"] == "online"

    def test_node_urls_point_at_device_pages(self, graph):
        for node in graph["nodes"]:
            assert node["url"] == f"/devices/{node['id']}"


class TestEdges:
    def test_expected_edges_only(self, graph):
        pairs = {tuple(sorted((e["source"], e["target"])))
                 for e in graph["edges"]}
        assert pairs == {
            ("AP1SERIAL", "SW1SERIAL"),   # switch -> AP neighbour
            ("SW1SERIAL", "SW2SERIAL"),   # deduped bidirectional link
        }

    def test_edge_field_contract(self, graph):
        for edge in graph["edges"]:
            assert set(edge) == EDGE_KEYS, edge

    def test_self_loops_skipped(self, graph):
        assert all(e["source"] != e["target"] for e in graph["edges"])

    def test_unknown_neighbour_serial_skipped(self, graph):
        ids = {n["id"] for n in graph["nodes"]}
        for e in graph["edges"]:
            assert e["source"] in ids and e["target"] in ids
        assert not any("GHOST" in (e["source"], e["target"])
                       for e in graph["edges"])

    def test_client_neighbours_not_wired(self, graph):
        assert not any("AA:11:22:33:44:55" in (e["source"], e["target"])
                       for e in graph["edges"])

    def test_bidirectional_report_deduped_to_one_edge(self, graph):
        sw_links = [e for e in graph["edges"]
                    if {e["source"], e["target"]} == {"SW1SERIAL", "SW2SERIAL"}]
        assert len(sw_links) == 1

    def test_edge_carries_port_and_speed(self, graph):
        ap_edge = next(e for e in graph["edges"]
                       if {e["source"], e["target"]} == {"AP1SERIAL", "SW1SERIAL"})
        assert ap_edge["port"] == "1/1/1"
        assert ap_edge["speed"] == 1_000_000_000


class TestDegradation:
    def test_unreachable_switch_counted_and_page_survives(self, client,
                                                          mock_central,
                                                          monkeypatch):
        from tests.conftest import RAW_PORTS
        from vendors import central_bridge as cb

        async def flaky_ports(serial):
            if serial == "SW2SERIAL":
                raise RuntimeError("switch unreachable")
            return list(RAW_PORTS.get(serial, []))

        monkeypatch.setattr(cb, "get_switch_ports", flaky_ports)
        r = client.get("/topology/")
        assert r.status_code == 200
        assert "Port data unavailable for 1 switch" in r.text

        g = extract_graph(r.text)
        # SW1's ports still produced its edges.
        pairs = {tuple(sorted((e["source"], e["target"]))) for e in g["edges"]}
        assert ("AP1SERIAL", "SW1SERIAL") in pairs

    def test_offline_switches_not_queried_for_ports(self, client, mock_central,
                                                    monkeypatch):
        from vendors import central_bridge as cb
        queried = []

        async def tracking_ports(serial):
            queried.append(serial)
            return []

        monkeypatch.setattr(cb, "get_switch_ports", tracking_ports)
        assert client.get("/topology/").status_code == 200
        assert "SW3SERIAL" not in queried          # offline switch skipped
        assert set(queried) == {"SW1SERIAL", "SW2SERIAL"}

    def test_device_counts_rendered(self, client, mock_central):
        r = client.get("/topology/")
        assert "5 devices" in r.text
