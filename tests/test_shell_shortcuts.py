"""Shell chrome: keyboard shortcuts overlay and sidebar wallboard."""


def test_shortcuts_overlay_lists_sites_and_wlans(client, mock_central, stub_db):
    r = client.get("/devices/")
    assert r.status_code == 200
    assert "Go Sites" in r.text
    assert "Go WLANs" in r.text
    assert "Keyboard shortcuts" in r.text


def test_sidebar_wallboard_button_on_desktop(client, mock_central, stub_db):
    r = client.get("/devices/")
    assert r.status_code == 200
    assert r.text.count('aria-label="Toggle NOC wallboard mode"') >= 2
    assert "hidden md:inline-flex" in r.text
