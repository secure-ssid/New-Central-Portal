"""The three Lab tools that replaced the demo pages.

The Lab had no test file at all — 13 of 16 tools were untested — so these also
establish the shape for the rest.
"""
import pytest


# ── Device Deep-Dive ─────────────────────────────────────────────────────────

def test_device_scope_without_a_device_offers_a_picker(client, mock_central, stub_db):
    r = client.get("/lab/device-scope")
    assert r.status_code == 200
    assert "Choose a device" in r.text
    assert "Undefined" not in r.text


def test_device_scope_renders_charts_as_server_side_svg(client, mock_central, stub_db):
    """No client JS computes these — the points come from Python."""
    r = client.get("/lab/device-scope?serial=SW1SERIAL")
    assert r.status_code == 200
    assert "<polyline" in r.text and 'points="' in r.text
    assert "Undefined" not in r.text


def test_switch_shows_the_physical_layer(client, mock_central, stub_db):
    r = client.get("/lab/device-scope?serial=SW1SERIAL").text
    assert "PoE budget" in r
    assert "Temperature" in r
    assert "Power loss or fault" in r, "restart reason should surface"


def test_an_access_point_has_no_physical_layer_section(client, mock_central, stub_db):
    """APs have no PoE, VLANs or spanning tree — absent, not empty."""
    r = client.get("/lab/device-scope?serial=AP1SERIAL").text
    assert "PoE budget" not in r
    assert "Spanning tree" not in r


def test_the_slow_diagnostics_never_auto_load(client, mock_central, stub_db):
    """They poll an async job with a 5s floor and are uncached; auto-firing
    them would hold a worker and a semaphore slot on every page view."""
    r = client.get("/lab/device-scope?serial=SW1SERIAL").text
    assert "/diagnostic" in r, "the buttons should exist"
    diagnostic_block = r[r.index("/diagnostic") - 400:r.index("/diagnostic") + 200]
    assert 'hx-trigger="load"' not in diagnostic_block


@pytest.mark.parametrize("path", [
    "/lab/device-scope/SW1SERIAL/poe",
    "/lab/device-scope/SW1SERIAL/vlans",
    "/lab/device-scope/SW1SERIAL/interface",
])
def test_fragments_return_fragments_not_pages(client, mock_central, stub_db, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "<html" not in r.text.lower()


def test_a_null_untagged_port_list_renders_a_dash(client, mock_central, stub_db):
    """`untaggedPorts: null` on a present row must not print "None"."""
    r = client.get("/lab/device-scope/SW1SERIAL/vlans").text
    assert ">None<" not in r
    assert "DEFAULT_VLAN_1" in r


def test_a_failing_trend_fetch_does_not_500(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("central down")

    monkeypatch.setattr(cb, "get_switch_hardware_trends", boom)
    monkeypatch.setattr(cb, "get_switch_details", boom)
    r = client.get("/lab/device-scope?serial=SW1SERIAL")
    assert r.status_code == 200
    assert "No trend data" in r.text


# ── Compliance Board ─────────────────────────────────────────────────────────

def test_compliance_flags_real_firmware_drift(client, mock_central, stub_db):
    r = client.get("/lab/compliance")
    assert r.status_code == 200
    assert "10.17.1020" in r.text
    assert "Update available" in r.text
    assert "Undefined" not in r.text


def test_a_blank_recommendation_is_not_drift(client, mock_central, stub_db):
    """lobby-ap-1 has no recommendedVersion — that is 'no advice', not drift."""
    r = client.get("/lab/compliance").text
    assert r.count("Update available") == 1


def test_compliance_reads_the_serial_field_not_serialnumber(client, mock_central, stub_db):
    """serialNumber is always null in the config-health payload."""
    r = client.get("/lab/compliance").text
    assert "SW1SERIAL" in r


def test_compliance_shows_central_insights(client, mock_central, stub_db):
    assert "Access Point Firmware Recommendation" in client.get("/lab/compliance").text


def test_compliance_survives_a_dead_upstream(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("central down")

    for name in ("list_firmware_upgrades", "list_devices_config_health", "list_insights"):
        monkeypatch.setattr(cb, name, boom)
    r = client.get("/lab/compliance")
    assert r.status_code == 200
    assert "Could not" in r.text or "no compliance data" in r.text


# ── Activity & History ───────────────────────────────────────────────────────

def test_activity_surfaces_cleared_alerts_with_root_cause(client, mock_central, stub_db):
    """These are invisible on /alerts/: list_active_alerts filters them out and
    every alert on this tenant is Cleared."""
    r = client.get("/lab/activity")
    assert r.status_code == 200
    assert "Config Out of Sync" in r.text
    assert "A configuration push did not complete." in r.text
    assert "Resync the device configuration." in r.text
    assert "Undefined" not in r.text


def test_an_empty_timeline_says_so_and_is_not_a_failure(client, mock_central, stub_db):
    """The table only records transitions, so empty is the normal state on a
    stable fleet. It must not read like a database problem."""
    r = client.get("/lab/activity").text
    assert "No transitions recorded yet" in r
    assert "database is unavailable" not in r


def test_a_dead_database_is_distinguishable_from_an_empty_timeline(
        client, mock_central, dead_db):
    r = client.get("/lab/activity")
    assert r.status_code == 200
    assert "database is unavailable" in r.text
    assert "No transitions recorded yet" not in r.text


def test_activity_shows_onboarding_events(client, mock_central, stub_db):
    assert "00:0c:29:a9:45:38" in client.get("/lab/activity").text


def test_malformed_root_cause_json_does_not_500(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def broken(*a, **k):
        return [{"id": "x", "name": "Broken", "severity": "Minor", "status": "Cleared",
                 "action": [{"rootCause": ["{not json"], "solution": None}]}]

    monkeypatch.setattr(cb, "list_all_alerts", broken)
    r = client.get("/lab/activity")
    assert r.status_code == 200
    assert "Broken" in r.text


# ── Application Visibility ───────────────────────────────────────────────────

def test_app_visibility_renders_the_watchlist_split(client, mock_central, stub_db):
    r = client.get("/lab/app-visibility")
    assert r.status_code == 200
    assert "Flagged and unclassified" in r.text
    assert "needost.shop" in r.text
    assert "Undefined" not in r.text


def test_the_small_unclassified_row_appears_before_the_big_known_one(
        client, mock_central, stub_db):
    """githubcopilot.com moves 10,000x more bytes and is equally flagged. If
    this ordering inverts, the page has gone back to burying its own signal."""
    r = client.get("/lab/app-visibility").text
    assert r.index("needost.shop") < r.index("githubcopilot.com")


def test_a_truncated_watchlist_says_how_much_it_cut(client, mock_central, stub_db,
                                                    monkeypatch):
    """A 7-day window flags ~135 recognised applications. Capping the table is
    right; capping it silently would read as "that is all of them"."""
    import routes.lab as lab
    from vendors import central_bridge as cb
    monkeypatch.setattr(lab, "_APP_WATCH_CAP", 1)

    async def many(*a, **k):
        return [{"name": f"app{i}.example", "risk": "MODERATE", "categories": ["Web"],
                 "rxBytes": 1000 - i, "txBytes": 0} for i in range(9)]

    monkeypatch.setattr(cb, "list_applications", many)
    r = client.get("/lab/app-visibility").text
    assert "Largest 1 of 9 by traffic" in r


def test_the_byte_caveat_is_on_the_page_not_only_in_a_comment(
        client, mock_central, stub_db):
    """DPI attribution on this tenant credits single applications with more
    traffic than the link plausibly carried, so the page must not present these
    numbers as measurements."""
    r = client.get("/lab/app-visibility").text
    assert "not interface counters" in r
    assert "Share of largest" in r, "the bars are relative, and must be labelled so"


def test_the_dead_fields_are_never_rendered(client, mock_central, stub_db):
    """experience, tlsVersion, certificateExpiryDate and state are empty or
    constant on every row of a 7-day window. A column for any of them would be
    permanently blank."""
    r = client.get("/lab/app-visibility").text
    for dead in ("Experience", "TLS version", "Certificate expiry", "ALLOWED"):
        assert dead not in r, f"{dead} has no data behind it"


def test_the_category_rollup_admits_it_double_counts(client, mock_central, stub_db):
    r = client.get("/lab/app-visibility").text
    assert "more than the whole" in r


def test_a_failed_fetch_is_distinguishable_from_a_quiet_window(
        client, mock_central, stub_db, monkeypatch):
    """The wrapper returns None for failure and [] for "nothing happened"."""
    from vendors import central_bridge as cb

    async def failed(*a, **k):
        return None

    monkeypatch.setattr(cb, "list_applications", failed)
    r = client.get("/lab/app-visibility")
    assert r.status_code == 200
    assert "did not return application data" in r.text

    async def quiet(*a, **k):
        return []

    monkeypatch.setattr(cb, "list_applications", quiet)
    r = client.get("/lab/app-visibility")
    assert r.status_code == 200
    assert "did not return application data" not in r.text
    assert "No applications seen in this window" in r.text


def test_an_out_of_range_window_falls_back_rather_than_400ing_upstream(
        client, mock_central, stub_db):
    """The endpoint rejects anything wider than 7 days, so 999 must never
    reach it."""
    r = client.get("/lab/app-visibility?hours=999")
    assert r.status_code == 200
    assert '<option value="24" selected>' in r.text


def test_a_dead_upstream_does_not_500(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def boom(*a, **k):
        raise RuntimeError("central down")

    monkeypatch.setattr(cb, "get_sites", boom)
    r = client.get("/lab/app-visibility")
    assert r.status_code == 200
    assert "Could not reach Aruba Central" in r.text


def test_the_client_drilldown_is_a_fragment_and_is_not_preloaded(
        client, mock_central, stub_db):
    page = client.get("/lab/app-visibility").text
    assert "/lab/app-visibility/client" in page
    block = page[page.index("/lab/app-visibility/client") - 300:
                 page.index("/lab/app-visibility/client") + 300]
    assert 'hx-trigger="load"' not in block

    r = client.get("/lab/app-visibility/client?mac=AA:11:22:33:44:55")
    assert r.status_code == 200
    assert "<html" not in r.text.lower()
    assert "Disney Plus" in r.text


def test_the_drilldown_shows_the_client_scope_not_the_site_scope(
        client, mock_central, stub_db):
    """A drilldown that quietly ignored client_id would still look right."""
    r = client.get("/lab/app-visibility/client?mac=AA:11:22:33:44:55").text
    assert "githubcopilot.com" not in r


def test_the_drilldown_without_a_mac_asks_for_one(client, mock_central, stub_db):
    r = client.get("/lab/app-visibility/client")
    assert r.status_code == 200
    assert "Pick a client" in r.text


# ── Menu integrity ───────────────────────────────────────────────────────────

def test_the_retired_demo_tools_are_gone(client, mock_central, stub_db):
    """Self-Healing Sim invented two faults and returned a static success div;
    Juniper Corner was three fixed notes behind a disabled button."""
    menu = client.get("/lab/").text
    assert "Self-Healing" not in menu
    # "Juniper" alone would match the CLI translator's description.
    assert "Juniper Corner" not in menu
    assert client.get("/lab/self-heal").status_code == 404
    assert client.get("/lab/juniper").status_code == 404


def test_the_new_tools_are_listed(client, mock_central, stub_db):
    menu = client.get("/lab/").text
    # Jinja escapes the ampersand, so match the rendered form.
    for name in ("Device Deep-Dive", "Compliance Board", "Activity &amp; History",
                 "Application Visibility"):
        assert name in menu, name


def test_the_chatbot_no_longer_claims_to_be_live(client, mock_central, stub_db):
    """Its GITHUB_TOKEN is the literal placeholder your_token_here, so it can
    only ever answer "not configured"."""
    import routes.lab as lab

    import asyncio
    from unittest.mock import MagicMock

    entries = None

    class _Capture:
        def TemplateResponse(self, request, name, ctx):
            nonlocal entries
            entries = ctx["experiments"]
            return MagicMock()

    original = lab.templates
    lab.templates = _Capture()
    try:
        asyncio.run(lab.lab_menu(MagicMock()))
    finally:
        lab.templates = original

    chat = next(e for e in entries if e["slug"] == "chat")
    assert chat["badge"] == "requires-token"
