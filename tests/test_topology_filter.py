"""Topology site filter tests."""
import json
import re

from routes.topology import _resolve_site_query


def test_resolve_site_query_by_name():
    site_map = {"hq": "101", "branch": "102"}
    name, sid = _resolve_site_query("HQ", site_map)
    assert sid == "101"
    assert name == "HQ"


def test_resolve_site_query_by_id():
    site_map = {"hq": "101"}
    name, sid = _resolve_site_query("101", site_map)
    assert sid == "101"


def _graph(html: str) -> dict:
    m = re.search(r"var RAW = (\{.*\});", html)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_topology_site_filter(client, mock_central):
    r = client.get("/topology/?site=HQ")
    assert r.status_code == 200
    assert "Site: HQ" in r.text
    g = _graph(r.text)
    assert g["nodes"]
    assert all(n.get("site") == "HQ" for n in g["nodes"])


def test_topology_site_filter_case_insensitive(client, mock_central):
    r = client.get("/topology/?site=hq")
    assert r.status_code == 200
    g = _graph(r.text)
    assert all(n.get("site") == "HQ" for n in g["nodes"])
