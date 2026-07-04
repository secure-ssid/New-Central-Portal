"""Smoke tests: every key page renders against mocked data, with auth off.

Assertions stick to stable structure (status codes, title block, known data
markers) — not pixel/styling details.
"""
import pytest


@pytest.mark.parametrize("path,markers", [
    ("/", ["Network Dashboard", 'id="dashboard-live"', "core-sw-1"]),
    ("/devices/", ["core-sw-1", "edge-sw-2", "branch-gw-1"]),
    ("/devices/SW1SERIAL", ["core-sw-1", "SW1SERIAL"]),
    ("/devices/AP1SERIAL", ["lobby-ap-1"]),
    ("/clients/", ["laptop-1", "printer-1"]),
    ("/clients/AA:11:22:33:44:55", ["laptop-1"]),
    ("/sites/", ["HQ", "Branch"]),
    ("/topology/", ["var RAW = {", "SW1SERIAL"]),
    ("/notifications/", ["ops@example.com"]),
    ("/lab/", ["Network Chatbot", "MCP Tool Tester"]),
    ("/lab/doc-api", ["OpenAPI Lookup"]),
    ("/lab/doc-ask", ["Documentation Q&A"]),
])
def test_page_renders(client, mock_central, stub_db, path, markers):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" in r.headers["content-type"]
    for marker in markers:
        assert marker in r.text, f"{path} missing {marker!r}"
    # No Jinja undefined leakage into the rendered page.
    assert "Undefined" not in r.text, f"{path} leaked a Jinja Undefined"


def test_pages_have_title_block(client, mock_central, stub_db):
    import re
    for path in ("/", "/devices/", "/clients/", "/sites/", "/topology/", "/lab/"):
        r = client.get(path)
        # Every page must fill the layout's <title> block with something.
        # (Branding text intentionally not pinned — devices/list.html currently
        # says "HPE Networking Portal" while the rest say "New Central Portal".)
        assert re.search(r"<title>\s*\S[^<]*</title>", r.text), path


def test_dashboard_partial_is_a_fragment(client, mock_central, stub_db):
    r = client.get("/?partial=1")
    assert r.status_code == 200
    # Fragment contains the live stats but not the page chrome.
    assert "Total Devices" in r.text
    assert "<html" not in r.text
    assert 'id="dashboard-live"' not in r.text


def test_healthz_db_ok(client, stub_db):
    assert client.get("/healthz").json() == {"status": "ok", "db": "ok"}


def test_healthz_db_down_still_200(client, dead_db):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "fail"}


def test_health_liveness(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_unknown_device_renders_themed_404(client, mock_central):
    r = client.get("/devices/NOSUCHSERIAL")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Page not found" in r.text


def test_unknown_url_renders_themed_404(client):
    r = client.get("/definitely/not/a/page")
    assert r.status_code == 404
    assert "Page not found" in r.text


def test_404_is_json_for_htmx(client, mock_central):
    r = client.get("/devices/NOSUCHSERIAL", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Device not found"


def test_404_is_json_for_api_accept(client):
    r = client.get("/nope", headers={"accept": "application/json"})
    assert r.status_code == 404
    assert "detail" in r.json()


def test_unhandled_error_renders_500_page_without_leaking(client, mock_central,
                                                          monkeypatch):
    from vendors.aruba_central import aruba

    async def explode(serial):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(aruba, "get_device", explode)
    r = client.get("/devices/SW1SERIAL")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text

    r = client.get("/devices/SW1SERIAL", headers={"HX-Request": "true"})
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal Server Error"}


def test_devices_page_survives_bridge_failure(client, monkeypatch, stub_db):
    """central_bridge completely broken -> mock-data fallback still renders."""
    from vendors import central_bridge as cb

    async def broken(*a, **k):
        raise RuntimeError("centralmcp unavailable")

    for fn in ("get_devices", "get_device_groups", "get_central_sites",
               "get_clients", "get_sites", "get_device_events",
               "get_glp_subscriptions"):
        monkeypatch.setattr(cb, fn, broken)

    r = client.get("/devices/")
    assert r.status_code == 200
    # aruba_central falls back to its built-in mock fleet.
    assert "CX6300-CORE" in r.text

    assert client.get("/").status_code == 200


def test_search_api_route_wired(client, mock_central):
    r = client.get("/search/api?q=core")
    assert r.status_code == 200
    assert "results" in r.json()


def test_lifespan_starts_degraded_with_dead_db(dead_db, monkeypatch):
    """App startup must survive a dead database (degraded mode by design).

    Regression guard: a function-local ``from config import settings`` inside
    lifespan() once shadowed the module import and crashed every startup with
    UnboundLocalError before this test existed.
    """
    import main
    from starlette.testclient import TestClient
    from config import settings

    monkeypatch.setattr(settings, "portal_password", "")
    # Entering the context manager runs the lifespan (DB init, scheduler).
    with TestClient(main.app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/healthz").json() == {"status": "ok", "db": "fail"}
