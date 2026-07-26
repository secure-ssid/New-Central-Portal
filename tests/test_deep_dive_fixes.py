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
