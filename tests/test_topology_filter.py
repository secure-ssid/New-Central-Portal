"""Topology site filter tests."""

import json
import re


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
