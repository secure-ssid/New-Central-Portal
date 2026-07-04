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
    assert "/devices/?site=HQ" in r.text
    assert "/clients/?site=HQ" in r.text


def test_ap_detail_site_link(client, mock_central, stub_db):
    r = client.get("/devices/AP1SERIAL")
    assert r.status_code == 200
    assert '/devices/?site=HQ' in r.text


def test_site_detail_not_found(client, mock_central, stub_db):
    r = client.get("/sites/nonexistent-site-id")
    assert r.status_code == 404


def test_site_list(client, mock_central, stub_db):
    r = client.get("/sites/")
    assert r.status_code == 200
    assert "HQ" in r.text
    assert "Branch" in r.text


def test_site_list_search(client, mock_central, stub_db):
    r = client.get("/sites/?q=branch")
    assert r.status_code == 200
    assert "Branch" in r.text
    assert "HQ" not in r.text


def test_site_list_pagination_metadata(client, mock_central, stub_db):
    r = client.get("/sites/?per_page=1")
    assert r.status_code == 200
    assert "Page 1 of 2" in r.text
