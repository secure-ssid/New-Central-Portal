"""Tests for site list and detail routes."""
from routes.sites import _health_fields


def test_health_fields_structured():
    rows = _health_fields({"status": "healthy", "deviceCount": 5, "clientCount": 12})
    assert rows[0]["label"] == "Overall"
    assert rows[0]["value"] == "healthy"
    values = {r["value"] for r in rows}
    assert "5" in values
    assert "12" in values


def test_site_detail_renders(client, mock_central, stub_db):
    r = client.get("/sites/101")
    assert r.status_code == 200
    assert "HQ" in r.text
    assert "Devices" in r.text


def test_site_detail_not_found(client, mock_central, stub_db):
    r = client.get("/sites/nonexistent-site-id")
    assert r.status_code == 404


def test_site_list(client, mock_central, stub_db):
    r = client.get("/sites/")
    assert r.status_code == 200
    assert "HQ" in r.text
    assert "Branch" in r.text
