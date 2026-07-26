"""Regression tests for the deep-dive fix batch.

Each fixture here uses the payload shape the LIVE tenant actually returns —
captured during the audit — not the invented shapes the older config-page
tests used (which is why several rendering bugs shipped green). The point of
these is that they fail against the pre-fix code and pass against the fix.
"""
import asyncio
import sys
import types

import pytest


# ── get_devices: client-side site/type filter (Central 400s on the query) ────

def _fake_inventory():
    return [
        {"serialNumber": "SW1", "deviceName": "core-sw-1", "deviceType": "SWITCH",
         "siteId": "79244870000394240", "siteName": "SecureSSID"},
        {"serialNumber": "AP1", "deviceName": "lobby-ap", "deviceType": "ACCESS_POINT",
         "siteId": "79244870000394240", "siteName": "SecureSSID"},
        {"serialNumber": "GW1", "deviceName": "branch-gw", "deviceType": "GATEWAY",
         "siteId": None, "siteName": None},
    ]


@pytest.fixture
def _stub_list_devices(monkeypatch):
    """Stub one layer below the wrapper: mcp_servers.monitoring.list_devices.

    Injected via sys.modules (mcp_servers is not importable in the test
    environment), the same pattern test_bridge_contract uses. Emulates the
    tenant: any siteId/deviceType query 400s and yields [], so the wrapper must
    filter the unfiltered records itself.
    """
    from vendors import central_bridge as cb
    cb.clear_bridge_cache()

    def list_devices(device_type=None, site_id=None, limit=50, offset=0, **_kw):
        if device_type or site_id:
            return []          # the 400 → [] the real endpoint produces
        return _fake_inventory()[:limit]

    monitoring = types.ModuleType("mcp_servers.monitoring")
    monitoring.list_devices = list_devices
    pkg = types.ModuleType("mcp_servers")
    pkg.monitoring = monitoring
    monkeypatch.setitem(sys.modules, "mcp_servers", pkg)
    monkeypatch.setitem(sys.modules, "mcp_servers.monitoring", monitoring)
    yield
    cb.clear_bridge_cache()


def test_site_filter_is_applied_client_side(_stub_list_devices):
    from vendors import central_bridge as cb
    devices = asyncio.run(cb.get_devices(site_id="79244870000394240"))
    serials = {d["serialNumber"] for d in devices}
    assert serials == {"SW1", "AP1"}, "site filter must match on siteId, and the "
    "gateway with siteId=None must be excluded"


def test_device_type_filter_matches_the_canonical_vocabulary(_stub_list_devices):
    from vendors import central_bridge as cb
    # 'AP' (the API spelling) must match a record whose deviceType is ACCESS_POINT.
    aps = asyncio.run(cb.get_devices(device_type="AP"))
    assert [d["serialNumber"] for d in aps] == ["AP1"]
    gws = asyncio.run(cb.get_devices(device_type="gateway"))
    assert [d["serialNumber"] for d in gws] == ["GW1"]


def test_unfiltered_still_returns_everything(_stub_list_devices):
    from vendors import central_bridge as cb
    assert len(asyncio.run(cb.get_devices(limit=200))) == 3


# ── /wlans/: essid-as-dict, vlan-id-range, enable ────────────────────────────

def test_wlan_page_renders_real_payload_shape(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def wlans(**_kw):
        # The shape the tenant actually sends.
        return [{"ssid": "Air-Pass", "essid": {"name": "Air Pass"},
                 "opmode": "WPA3_ENTERPRISE_CCM_128", "type": "employee",
                 "vlan-id-range": ["200"], "enable": True}]

    monkeypatch.setattr(cb, "list_wlans", wlans)
    r = client.get("/wlans/").text
    assert "Air Pass" in r
    assert "{'name'" not in r, "the essid dict must be unwrapped, not rendered raw"
    assert "200" in r, "the VLAN must come from vlan-id-range"


def test_a_disabled_wlan_is_not_forced_enabled(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def wlans(**_kw):
        return [{"ssid": "Old", "essid": {"name": "Old"}, "enable": False,
                 "vlan-id-range": ["1"]}]

    monkeypatch.setattr(cb, "list_wlans", wlans)
    r = client.get("/wlans/").text
    # The old default (status != "disabled") rendered everything enabled.
    assert "Old" in r


# ── /platform/nac: displayName, enable, staticTags ───────────────────────────

def test_nac_renders_displayname_and_enable(client, mock_central, stub_db, monkeypatch):
    from vendors import central_bridge as cb

    async def regs(**_kw):
        return [{"macAddress": "00:0c:29:a9:45:38", "displayName": "phone-test",
                 "enable": True, "staticTags": [], "type": "static"}]

    monkeypatch.setattr(cb, "list_mac_registrations", regs)
    r = client.get("/platform/nac").text
    assert "phone-test" in r, "description must read displayName"
    assert "enabled" in r.lower()


# ── GLP service offers: rows nested under data.items ─────────────────────────

def test_glp_service_offers_unwraps_the_data_envelope(monkeypatch):
    from vendors import central_bridge as cb
    cb.clear_bridge_cache()

    def offers(limit=50):
        return {"data": {"count": 2, "total": 2, "next": None,
                         "items": [{"id": "o1"}, {"id": "o2"}]},
                "endpoint_used": "x", "errors": []}

    glp = types.ModuleType("mcp_servers.glp")
    glp.list_glp_service_offers = offers
    pkg = types.ModuleType("mcp_servers")
    pkg.glp = glp
    monkeypatch.setitem(sys.modules, "mcp_servers", pkg)
    monkeypatch.setitem(sys.modules, "mcp_servers.glp", glp)
    try:
        rows = asyncio.run(cb.list_glp_service_offers())
        assert [r["id"] for r in rows] == ["o1", "o2"]
    finally:
        cb.clear_bridge_cache()


# ── Firmware compliance: unknown is not drift ────────────────────────────────

def test_firmware_no_data_device_is_unknown_not_a_verdict():
    """A device with neither current nor recommended version is 'unknown' —
    the old rule marked it 'compliant', inflating the compliant count; a device
    with a target but null current was marked 'non-compliant', inflating drift.
    """
    from routes.platform import _normalize_firmware_compliance
    raw = {"items": [
        {"deviceName": "has-data", "firmwareVersion": "10.1", "recommendedVersion": "10.2",
         "complianceStatus": "non-compliant"},
        {"deviceName": "up-to-date", "firmwareVersion": "10.2", "recommendedVersion": "10.2",
         "complianceStatus": "compliant"},
        {"deviceName": "no-data", "firmwareVersion": None, "recommendedVersion": None,
         "complianceStatus": "unknown"},
    ]}
    out = _normalize_firmware_compliance(raw)
    s = out["summary"]
    assert s == {"total": 3, "compliant": 1, "unknown": 1, "non_compliant": 1}


# ── Connection pool + DSN ────────────────────────────────────────────────────

def test_pool_is_prewarmed_not_a_pool_of_one(monkeypatch):
    """minconn must equal maxconn: psycopg2 closes any connection above minconn
    on putconn, so minconn=1 made it a pool of one (~100x slower checkout)."""
    import db
    from psycopg2 import pool as pgpool

    captured = {}

    class _FakePool:
        def __init__(self, minconn, maxconn, **kw):
            captured["minconn"] = minconn
            captured["maxconn"] = maxconn

    monkeypatch.setattr(pgpool, "ThreadedConnectionPool", _FakePool)
    monkeypatch.setattr(db, "_pool", None)
    db.get_pool()
    assert captured == {"minconn": 10, "maxconn": 10}
    monkeypatch.setattr(db, "_pool", None)


def test_parse_dsn_adds_timeouts_and_keeps_query_params():
    import db
    parsed = db._parse_dsn(
        "postgresql://u:p@h:5433/mydb?sslmode=require&application_name=custom")
    assert parsed["connect_timeout"] == 5
    assert parsed["keepalives"] == 1
    # An explicit query param wins over the default.
    assert parsed["application_name"] == "custom"
    assert parsed["sslmode"] == "require"
    assert parsed["host"] == "h" and parsed["port"] == 5433 and parsed["dbname"] == "mydb"


# ── Running-config secret masking (ops_format.mask_config_secrets) ───────────

def test_masking_redacts_ciphertext_but_keeps_the_directive():
    from ops_format import mask_config_secrets
    src = "user admin password ciphertext AQBapaB6xY9zzKKuh7l0Q2\ninterface 1/1/1"
    out = mask_config_secrets(src)
    assert "AQBapaB6xY9zzKKuh7l0Q2" not in out
    assert "password ciphertext" in out          # directive preserved
    assert "interface 1/1/1" in out               # non-secret untouched


def test_masking_covers_community_and_passphrase():
    from ops_format import mask_config_secrets
    out = mask_config_secrets(
        "snmp-server community S3cr3tRO\nwlan wpa-passphrase MyWiFiPass123")
    assert "S3cr3tRO" not in out and "MyWiFiPass123" not in out
    assert "snmp-server community" in out


def test_masking_is_a_noop_on_clean_config():
    from ops_format import mask_config_secrets
    src = "hostname core-sw-1\nvlan 200\n    name data"
    assert mask_config_secrets(src) == src
    assert mask_config_secrets("") == ""


# ── format_ops_response unwraps the ops-job envelope ─────────────────────────

def test_ops_job_envelope_renders_command_output_not_raw_json():
    """The show/diagnostic tools nest output under output.results[].output.
    Before, this fell through to a raw JSON dump."""
    from ops_format import format_ops_response
    envelope = {
        "status": "COMPLETED",
        "output": {"commands": ["show lldp neighbor"],
                   "results": [{"command": "show lldp neighbor",
                                "output": "Total Neighbor Entries : 10"}]},
        "errors": [],
    }
    body = format_ops_response(envelope).body.decode()
    assert "Total Neighbor Entries : 10" in body
    assert "show lldp neighbor" in body
    assert '"status"' not in body and "COMPLETED" not in body  # no raw JSON dump


def test_ops_job_masks_secrets_in_command_output():
    from ops_format import format_ops_response
    envelope = {"output": {"results": [
        {"command": "show running-config",
         "output": "user admin password ciphertext AQBsecretblob123"}]}}
    body = format_ops_response(envelope).body.decode()
    assert "AQBsecretblob123" not in body
    assert "password ciphertext" in body


def test_ops_job_falls_through_when_not_a_job_envelope():
    """A plain structured dict must still render as a key/value table."""
    from ops_format import format_ops_response
    body = format_ops_response({"model": "CX6300", "uptime": "5d"}).body.decode()
    assert "CX6300" in body and "uptime" in body


# ── LLDP / Find-MAC use the CX-accepted command strings ──────────────────────

def test_find_mac_in_table_parses_port_and_vlan():
    from vendors.central_bridge import _find_mac_in_table
    env = {"output": {"results": [{"command": "show mac-address-table", "output":
        "MAC Address          VLAN     Type        Port\n"
        "-------------------------------------------------\n"
        "94:40:c9:12:71:d2    1        dynamic     1/1/23\n"
        "00:0b:86:b8:c4:b8    200      dynamic     1/1/17\n"}]}}
    hit = _find_mac_in_table(env, "00:0b:86:b8:c4:b8")
    assert hit["port"] == "1/1/17" and hit["vlan"] == "200"
    # Case/separator-insensitive.
    assert _find_mac_in_table(env, "944 0.c912.71d2")["port"] == "1/1/23"


def test_find_mac_absent_returns_a_readable_message_not_a_false_hit():
    from vendors.central_bridge import _find_mac_in_table
    env = {"output": {"results": [{"command": "show mac-address-table",
        "output": "MAC Address  VLAN  Type  Port\naa:bb:cc:00:11:22  5  dynamic  1/1/1\n"}]}}
    res = _find_mac_in_table(env, "de:ad:be:ef:00:00")
    assert "port" not in res  # no false match
    assert "not in the forwarding table" in res["output"]["results"][0]["output"]


def test_mac_key_strips_separators_and_case():
    from vendors.central_bridge import _mac_key
    for form in ("00:0B:86:B8:C4:B8", "000b.86b8.c4b8", "00-0b-86-b8-c4-b8"):
        assert _mac_key(form) == "000b86b8c4b8"


# ── Ping panel: structured CX stats, coloured by real reachability ───────────

def test_ping_structured_stats_render_reachable_green():
    from ops_format import format_ping_response
    env = {"status": "COMPLETED", "output": {
        "destination": "8.8.8.8", "resolvedIp": "8.8.8.8",
        "transmittedPacketsCount": "3", "receivedPacketsCount": "3",
        "packetLossPercent": "0", "averageRoundTripTimeMilliseconds": "15.670",
        "minimumRoundTripTimeMilliseconds": "14.191",
        "maximumRoundTripTimeMilliseconds": "16.764"}}
    body = format_ping_response(env).body.decode()
    assert "Reachable" in body and "#4ade80" in body      # green
    assert "3 sent" in body and "0% loss" in body
    assert "15.67 ms" in body                              # avg RTT
    assert "transmittedPacketsCount" not in body           # no raw dict dump


def test_ping_full_loss_renders_unreachable_red():
    from ops_format import format_ping_response
    env = {"output": {"destination": "10.0.0.9", "transmittedPacketsCount": "3",
                      "receivedPacketsCount": "0", "packetLossPercent": "100"}}
    body = format_ping_response(env).body.decode()
    assert "Unreachable" in body and "#f87171" in body     # red


def test_ping_text_output_success_detection():
    """A CLI-text ping (AP/gateway) with 0% loss must render green, even though
    the word 'success' never appears — the old bug."""
    from ops_format import format_ping_response
    env = {"output": {"results": [{"command": "ping 1.1.1.1",
        "output": "5 packets transmitted, 5 received, 0% packet loss"}]}}
    body = format_ping_response(env).body.decode()
    assert "#4ade80" in body and "0% packet loss" in body


# ── AP wireless card: real RF values, aggregated util, 0 != dash ─────────────

def test_wireless_card_reads_real_rf_keys_and_aggregates_util():
    from routes.devices import _wireless_cards
    ap_radios = {"radios": [
        {"band": "5 GHz", "channel": "104E", "channelUtilization": "3",
         "noiseFloor": "-99", "power": "15", "channelQuality": "99", "clientCount": 1},
        {"band": "2.4 GHz", "channel": "6", "channelUtilization": "11",
         "noiseFloor": "-92", "power": "8", "channelQuality": "94", "clientCount": 0},
    ]}
    cards = _wireless_cards(None, ap_radios, None)
    r0 = cards["radios"][0]
    assert r0["util"] == "3" and r0["noise"] == "-99" and r0["power"] == "15"
    assert r0["quality"] == "99"
    # 0 clients renders "0", not "—"
    assert cards["radios"][1]["clients"] == "0"
    # channel-util summary is the average of the two radios (3, 11 -> 7)
    assert cards["channel_util_pct"] == "7"


def test_wireless_metrics_unwraps_the_metrics_envelope():
    from routes.devices import _wireless_cards
    env = {"serial_number": "x", "endpoint_used": "y", "errors": [],
           "metrics": {"mode": "Client Access", "currentUplinkInUse": "Ethernet",
                       "lastRebootReason": "Power-reset", "meshRole": "-"}}
    cards = _wireless_cards(env, None, None)
    labels = {m["label"]: m["value"] for m in cards["metrics"]}
    assert labels["Mode"] == "Client Access"
    assert labels["Uplink"] == "Ethernet"
    assert "Mesh role" not in labels          # "-" placeholder skipped


def test_wireless_card_empty_when_nothing_reported():
    from routes.devices import _wireless_cards
    cards = _wireless_cards(None, None, None)
    assert cards == {"radios": [], "metrics": [], "channel_util_pct": None}


# ── MAC table: parse the text into rows (no blank row) ───────────────────────

def test_parse_mac_table_extracts_rows_and_skips_chrome():
    from ops_format import parse_mac_table
    text = (
        "MAC age-time            : 300 seconds\n"
        "Number of MAC addresses : 3\n"
        "\n"
        "MAC Address          VLAN     Type        Port\n"
        "-----------------------------------------------\n"
        "94:40:c9:12:71:d2    1        dynamic     1/1/23\n"
        "f4:e1:fc:c9:4f:a0    5        dynamic     1/1/15\n"
        "00:0b:86:b8:c4:b8    200      dynamic     1/1/17\n"
    )
    rows = parse_mac_table(text)
    assert len(rows) == 3          # header/separator/summary all skipped
    assert rows[0] == {"mac": "94:40:c9:12:71:d2", "vlan": "1",
                       "type": "dynamic", "port": "1/1/23"}
    assert rows[2]["port"] == "1/1/17" and rows[2]["vlan"] == "200"


def test_parse_mac_table_empty_input_is_no_rows_not_a_blank_row():
    from ops_format import parse_mac_table
    assert parse_mac_table("") == []
    assert parse_mac_table("MAC Address  VLAN  Type  Port\n----\n") == []


# ── Bad query param on an HTML page -> themed 400, JSON stays JSON ────────────

def test_bad_query_param_on_html_page_renders_themed_400(client, mock_central, stub_db):
    """?hours=abc (hours is an int) used to return a raw 422 JSON blob to a
    browser navigating an HTML page."""
    r = client.get("/lab/app-visibility?hours=abc", headers={"accept": "text/html"})
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
    assert "Bad request" in r.text
    assert "int_parsing" not in r.text          # no raw validation JSON


def test_bad_query_param_for_api_client_stays_json(client, mock_central, stub_db):
    r = client.get("/lab/app-visibility?hours=abc",
                   headers={"accept": "application/json"})
    assert r.status_code == 422
    assert "application/json" in r.headers["content-type"]


def test_bad_query_param_for_htmx_stays_json(client, mock_central, stub_db):
    r = client.get("/lab/activity?hours=xyz", headers={"hx-request": "true"})
    assert r.status_code == 422
    assert "application/json" in r.headers["content-type"]


# ── asset_url memoization + notifications db-error banner ─────────────────────

def test_asset_url_memoizes_until_the_file_changes(tmp_path, monkeypatch):
    import templates_shared as ts
    ts._asset_hash_cache.clear()
    # Point "static" at a temp dir with a known file.
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.css").write_bytes(b"body{}")
    monkeypatch.chdir(tmp_path)

    reads = {"n": 0}
    real_open = open

    def counting_open(path, *a, **k):
        if str(path).endswith("app.css") and "b" in (a[0] if a else k.get("mode", "")):
            reads["n"] += 1
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", counting_open)
    u1 = ts.asset_url("/static/app.css")
    u2 = ts.asset_url("/static/app.css")
    u3 = ts.asset_url("/static/app.css")
    assert u1 == u2 == u3 and "?v=" in u1
    assert reads["n"] == 1, "the file should be hashed once, not on every call"


def test_asset_url_missing_file_serves_unversioned(monkeypatch, tmp_path):
    import templates_shared as ts
    ts._asset_hash_cache.clear()
    monkeypatch.chdir(tmp_path)
    assert ts.asset_url("/static/nope.css") == "/static/nope.css"


def test_notifications_db_error_banner_renders(client, mock_central, dead_db):
    """The route computes `warning` on DB failure; the template must show it."""
    r = client.get("/notifications/")
    assert r.status_code == 200
    assert "Database unavailable" in r.text


# ── Lab MCP tester dispatch is cached; notification history is deterministic ─

def test_run_tool_is_cached_so_repeated_clicks_do_not_re_hit_upstream(monkeypatch):
    import sys
    import types
    from vendors import central_bridge as cb
    cb.clear_bridge_cache()

    calls = {"n": 0}

    def list_sites(**kw):
        calls["n"] += 1
        return {"items": [{"id": "1", "scopeName": "S"}]}

    monitoring = types.ModuleType("mcp_servers.monitoring")
    monitoring.list_sites = list_sites
    pkg = types.ModuleType("mcp_servers")
    pkg.monitoring = monitoring
    monkeypatch.setitem(sys.modules, "mcp_servers", pkg)
    monkeypatch.setitem(sys.modules, "mcp_servers.monitoring", monitoring)

    import asyncio
    r1 = asyncio.run(cb.run_tool("list_sites", "{}"))
    r2 = asyncio.run(cb.run_tool("list_sites", "{}"))
    assert r1["status"] == "success" and r1 == r2
    assert calls["n"] == 1, "identical (tool, params) must be served from cache"
    # Different params => different key => a second upstream call.
    asyncio.run(cb.run_tool("list_sites", '{"limit": 5}'))
    assert calls["n"] == 2
    cb.clear_bridge_cache()


def test_run_tool_invalid_json_is_a_clean_error_not_a_crash():
    import asyncio
    from vendors import central_bridge as cb
    cb.clear_bridge_cache()
    r = asyncio.run(cb.run_tool("whatever", "{not json"))
    assert r["status"] == "error" and "Invalid JSON" in r["error"]


def test_notification_history_orders_by_id_tiebreak():
    """Same-second rows must order deterministically, like the sibling getters."""
    import inspect
    import db
    src = inspect.getsource(db.get_notification_history)
    assert "ORDER BY sent_at DESC, id DESC" in src


# ── warm_cache warms the keys the routes actually read (D5) ──────────────────

def test_warm_cache_uses_route_matching_arguments(monkeypatch):
    from vendors import central_bridge as cb
    cb.clear_bridge_cache()
    calls: dict = {}

    def recorder(name, ret=None):
        async def f(*a, **k):
            calls.setdefault(name, []).append((a, k))
            return ret if ret is not None else []
        return f

    # One site that only carries New Central's `scopeName`, and one device.
    monkeypatch.setattr(cb, "get_sites", recorder("get_sites",
        ret=[{"scopeId": "79244870000394240", "scopeName": "SecureSSID"}]))
    monkeypatch.setattr(cb, "get_devices", recorder("get_devices",
        ret=[{"serialNumber": "AP1", "deviceType": "ACCESS_POINT",
              "status": "Up", "siteId": "79244870000394240"}]))
    for name in ("get_clients", "list_active_alerts", "get_tenant_health",
                 "get_fleet_health", "get_site_health_summary", "get_device_events"):
        monkeypatch.setattr(cb, name, recorder(name))

    asyncio.run(cb.warm_cache())

    # Alerts warmed at the limit the routes use (100), not the default 50.
    assert calls["list_active_alerts"][0][1]["limit"] == 100
    # Site health warmed with the name resolved from scopeName (was None before).
    assert calls["get_site_health_summary"][0][1]["site_name"] == "SecureSSID"
    # Events warmed with the same hours/limit and normalized type the route uses.
    ev = calls["get_device_events"][0]
    assert ev[1]["hours"] == 24 and ev[1]["limit"] == 10
    assert ev[1]["device_type"] == "access_point"    # normalized, not ACCESS_POINT
    cb.clear_bridge_cache()
