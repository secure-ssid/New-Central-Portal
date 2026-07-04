"""WLAN list route tests."""

from bridge_errors import BRIDGE_UNAVAILABLE


def test_wlans_page_renders(client, mock_central, stub_db):
    r = client.get("/wlans/")
    assert r.status_code == 200
    assert "WLAN Inventory" in r.text


def test_wlans_search_filter(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def wlans(**_kw):
        return [
            {"name": "corp-wifi", "essid": "corp-wifi", "security": "wpa2", "vlanId": 20, "enabled": True},
            {"name": "guest", "essid": "guest-wifi", "security": "open", "vlanId": 99, "enabled": False},
        ]

    monkeypatch.setattr(cb, "list_wlans", wlans)
    r = client.get("/wlans/?q=guest")
    assert r.status_code == 200
    assert "guest-wifi" in r.text
    assert "corp-wifi" not in r.text


def test_wlans_bridge_error_sanitized(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("internal wlan failure")

    monkeypatch.setattr(cb, "list_wlans", boom)
    r = client.get("/wlans/")
    assert r.status_code == 200
    assert "internal wlan failure" not in r.text
    assert BRIDGE_UNAVAILABLE in r.text


def test_wlans_pagination(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def wlans(**_kw):
        return [
            {"name": f"wlan-{i}", "essid": f"ssid-{i}", "security": "wpa2", "enabled": True}
            for i in range(5)
        ]

    monkeypatch.setattr(cb, "list_wlans", wlans)
    r = client.get("/wlans/?per_page=2")
    assert r.status_code == 200
    assert "Page 1 of 3" in r.text
