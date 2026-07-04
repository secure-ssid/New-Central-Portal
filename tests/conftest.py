"""Shared fixtures: TestClients (auth on/off), deterministic central_bridge
mocks, in-memory DB stubs, and a dead-DB environment.

Everything is hermetic: no network, no live Postgres. The real ``centralmcp``
package is never importable in CI, so any code path that genuinely calls it
raises ImportError — which the app is designed to tolerate — but the fixtures
below replace the bridge functions with deterministic data anyway.
"""
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

import db as db_module
import security
from config import settings
from routes import auth as auth_routes

# NOTE: ``main`` is imported lazily inside the client fixtures — importing it
# mounts StaticFiles, which requires the cwd to already be ``app/`` (done in
# the repo-root conftest's pytest_sessionstart, after this file is loaded).

TEST_PASSWORD = "unit-test-password"
TEST_SECRET = "unit-test-session-secret"

NOW = datetime.now(timezone.utc)


# ── Deterministic central_bridge dataset ─────────────────────────────────────
# Raw (centralmcp-shaped) payloads; the app normalises them via _norm_device /
# _norm_client, so tests exercise the real normalisation code.

RAW_DEVICES = [
    {"serialNumber": "SW1SERIAL", "deviceName": "core-sw-1", "deviceType": "SWITCH",
     "status": "Up", "ipv4": "10.0.0.2", "macAddress": "aa:bb:cc:00:00:01",
     "model": "6300M", "siteName": "HQ"},
    {"serialNumber": "SW2SERIAL", "deviceName": "edge-sw-2", "deviceType": "CX",
     "status": "Up", "ipv4": "10.0.0.3", "macAddress": "aa:bb:cc:00:00:02",
     "model": "6200F", "siteName": "HQ"},
    {"serialNumber": "AP1SERIAL", "deviceName": "lobby-ap-1", "deviceType": "AP",
     "status": "Up", "ipv4": "10.0.0.10", "macAddress": "aa:bb:cc:00:00:03",
     "model": "AP-635", "siteName": "HQ"},
    {"serialNumber": "GW1SERIAL", "deviceName": "branch-gw-1", "deviceType": "GATEWAY",
     "status": "Down", "ipv4": "10.1.0.1", "macAddress": "aa:bb:cc:00:00:04",
     "model": "9004", "siteName": "Branch"},
    {"serialNumber": "SW3SERIAL", "deviceName": "branch-sw-3", "deviceType": "SWITCH",
     "status": "Down", "ipv4": "10.1.0.2", "macAddress": "aa:bb:cc:00:00:05",
     "model": "6100", "siteName": "Branch"},
]

RAW_CLIENTS = [
    {"macAddress": "AA:11:22:33:44:55", "ipv4": "10.0.0.50", "hostName": "laptop-1",
     "userName": "alice", "status": "CONNECTED", "clientConnectionType": "WIRELESS",
     "connectedTo": "lobby-ap-1", "connectedDeviceSerial": "AP1SERIAL",
     "wlanName": "corp-wifi", "vlanId": 20, "siteName": "HQ"},
    {"macAddress": "BB:11:22:33:44:66", "ipv4": "10.0.0.51", "hostName": "printer-1",
     "userName": "", "status": "CONNECTED", "clientConnectionType": "wired",
     "connectedTo": "core-sw-1", "connectedDeviceSerial": "SW1SERIAL",
     "port": "1/1/4", "vlanId": 5, "siteName": "HQ"},
]

# Switch-port tables keyed by switch serial. SW1 deliberately includes a
# self-loop, an unknown neighbour, and a client neighbour so topology tests
# can prove they are skipped.
RAW_PORTS = {
    "SW1SERIAL": [
        {"name": "1/1/1", "index": 1, "portAlignment": "Top", "status": "Connected",
         "speed": 1_000_000_000, "poeStatus": "Delivering", "uplink": False,
         "neighbour": "lobby-ap-1", "neighbourSerial": "AP1SERIAL",
         "neighbourType": "Access Point"},
        {"name": "1/1/2", "index": 2, "portAlignment": "Bottom", "status": "Down",
         "speed": 0, "poeStatus": "Not Used"},
        {"name": "1/1/3", "index": 3, "portAlignment": "Top", "status": "Connected",
         "speed": 10_000_000_000, "uplink": True,
         "neighbourSerial": "SW2SERIAL", "neighbourType": "Switch"},
        # Self-loop — must never become an edge.
        {"name": "1/1/4", "index": 4, "portAlignment": "Bottom", "status": "Connected",
         "neighbourSerial": "SW1SERIAL", "neighbourType": "Switch"},
        # Neighbour we have no node for — must be skipped.
        {"name": "1/1/5", "index": 5, "portAlignment": "Top", "status": "Connected",
         "neighbourSerial": "GHOSTSERIAL", "neighbourType": "Switch"},
        # Client MAC neighbour — wrong type, must be skipped.
        {"name": "1/1/6", "index": 6, "portAlignment": "Bottom", "status": "Connected",
         "neighbourSerial": "AA:11:22:33:44:55", "neighbourType": "client"},
    ],
    "SW2SERIAL": [
        # Reverse report of the SW1<->SW2 link — must be deduplicated.
        {"name": "1/1/1", "index": 1, "portAlignment": "Top", "status": "Connected",
         "speed": 10_000_000_000, "neighbourSerial": "SW1SERIAL",
         "neighbourType": "Switch"},
    ],
}

RAW_EVENTS = [
    {"eventName": "Device rebooted", "description": "Reboot requested by admin",
     "category": "system", "timeAt": (NOW - timedelta(minutes=5)).isoformat()},
    {"eventName": "Port up", "description": "Interface 1/1/1 came up",
     "category": "interface", "timeAt": (NOW - timedelta(hours=2)).isoformat()},
]

RAW_SITES = [
    {"id": 101, "siteName": "HQ", "associated_device_count": 3, "client_count": 2,
     "address": "123 Main St", "city": "Memphis", "state": "TN"},
    {"id": 102, "siteName": "Branch", "associated_device_count": 2, "client_count": 0,
     "address": "9 Side Rd", "city": "Nashville", "state": "TN"},
]

CLASSIC_SITES = [
    {"site_id": 101, "site_name": "HQ", "city": "Memphis", "state": "TN",
     "address": "123 Main St"},
    {"site_id": 102, "site_name": "Branch", "city": "Nashville", "state": "TN",
     "address": "9 Side Rd"},
]


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


@pytest.fixture
def mock_central(monkeypatch):
    """Replace every central_bridge accessor the routes use with deterministic
    in-memory data. Returns the raw dataset for assertions."""
    from vendors import central_bridge as cb

    async def get_devices(device_type=None, site_id=None, limit=50, offset=0, **_kw):
        return list(RAW_DEVICES)

    async def get_device(serial):
        return next((d for d in RAW_DEVICES if d["serialNumber"] == serial), None)

    async def get_clients(site_id=None, limit=100, offset=0, **_kw):
        return list(RAW_CLIENTS)

    async def get_all_devices(limit_per_page=200, max_items=1000, **_kw):
        return list(RAW_DEVICES)

    async def get_all_clients(limit_per_page=200, max_items=1000, **_kw):
        return list(RAW_CLIENTS)

    async def find_client(mac_or_ip):
        return next((c for c in RAW_CLIENTS
                     if c["macAddress"] == mac_or_ip or c["ipv4"] == mac_or_ip), None)

    async def get_switch_ports(serial):
        return list(RAW_PORTS.get(serial, []))

    async def get_device_events(serial, hours=24, limit=20):
        return list(RAW_EVENTS)

    monkeypatch.setattr(cb, "get_devices", get_devices)
    monkeypatch.setattr(cb, "get_all_devices", get_all_devices)
    monkeypatch.setattr(cb, "get_device", get_device)
    monkeypatch.setattr(cb, "get_clients", get_clients)
    monkeypatch.setattr(cb, "get_all_clients", get_all_clients)
    monkeypatch.setattr(cb, "find_client", find_client)
    monkeypatch.setattr(cb, "get_switch_ports", get_switch_ports)
    monkeypatch.setattr(cb, "get_device_events", get_device_events)
    monkeypatch.setattr(cb, "get_sites", _async_return(list(RAW_SITES)))
    monkeypatch.setattr(cb, "get_classic_sites", _async_return(list(CLASSIC_SITES)))
    monkeypatch.setattr(cb, "get_central_sites", _async_return(list(CLASSIC_SITES)))
    monkeypatch.setattr(cb, "get_device_groups",
                        _async_return([{"groupName": "default"}, {"groupName": "lab"}]))
    monkeypatch.setattr(cb, "get_glp_subscriptions", _async_return([]))
    monkeypatch.setattr(cb, "get_alerts", _async_return([]))
    monkeypatch.setattr(cb, "list_active_alerts", _async_return([]))
    monkeypatch.setattr(cb, "get_site_health_summary", _async_return({"status": "ok"}))
    monkeypatch.setattr(cb, "get_tenant_health", _async_return({"status": "healthy"}))
    monkeypatch.setattr(cb, "get_client_details", _async_return({}))
    monkeypatch.setattr(cb, "locate_client", _async_return({}))
    monkeypatch.setattr(cb, "get_client_roaming_history", _async_return({"events": []}))
    monkeypatch.setattr(cb, "list_wlans", _async_return([]))
    monkeypatch.setattr(cb, "list_mac_registrations", _async_return([]))
    monkeypatch.setattr(cb, "get_firmware_compliance", _async_return({}))
    monkeypatch.setattr(cb, "list_glp_service_offers", _async_return([]))
    monkeypatch.setattr(cb, "detect_client_flapping", _async_return({}))
    monkeypatch.setattr(cb, "detect_ssh_brute_force", _async_return({}))
    monkeypatch.setattr(cb, "get_device_health", _async_return({"health": None, "errors": []}))
    monkeypatch.setattr(cb, "find_device_uplink", _async_return(
        {"switch_serial": "SW1SERIAL", "switch_name": "core-sw-1", "port": "1/1/1"}))
    return {
        "devices": RAW_DEVICES, "clients": RAW_CLIENTS, "ports": RAW_PORTS,
        "events": RAW_EVENTS, "sites": RAW_SITES, "classic_sites": CLASSIC_SITES,
    }


# ── DB stubs ─────────────────────────────────────────────────────────────────

class FakeBellStore:
    """In-memory stand-in for the in_app_notifications helpers in db.py."""

    def __init__(self):
        self.rows: list[dict] = []
        self._next_id = 1

    def add(self, title, body="", severity="info", device_serial=None, url=None,
            created_at=None, read=False):
        self.rows.append({
            "id": self._next_id, "title": title, "body": body, "severity": severity,
            "device_serial": device_serial, "url": url,
            "created_at": created_at or datetime.now(timezone.utc), "read": read,
        })
        self._next_id += 1

    def get_recent(self, limit=15):
        ordered = sorted(self.rows, key=lambda r: (r["created_at"], r["id"]),
                         reverse=True)
        return [dict(r) for r in ordered[:limit]]

    def count_unread(self):
        return sum(1 for r in self.rows if not r["read"])

    def mark_read(self, ids=None, mark_all=False):
        for r in self.rows:
            if mark_all or (ids and r["id"] in ids):
                r["read"] = True
        return self.count_unread()


@pytest.fixture
def bell_store(monkeypatch):
    store = FakeBellStore()
    monkeypatch.setattr(db_module, "get_in_app_notifications", store.get_recent)
    monkeypatch.setattr(db_module, "count_unread_notifications", store.count_unread)
    monkeypatch.setattr(
        db_module, "mark_notifications_read",
        lambda ids=None, mark_all=False: store.mark_read(ids=ids, mark_all=mark_all),
    )
    return store


@pytest.fixture
def stub_db(monkeypatch, bell_store):
    """Benign in-memory DB stubs for page rendering (settings/recipients/rules)."""
    setting_defaults = {
        "thresholds": "90,60,30,15", "smtp_port": "587", "smtp_tls": "true",
        "check_subscriptions": "true", "check_ssl": "true", "ssl_hosts": "",
    }
    monkeypatch.setattr(db_module, "get_setting",
                        lambda key: setting_defaults.get(key, ""))
    monkeypatch.setattr(db_module, "get_recipients",
                        lambda: [{"email": "ops@example.com", "active": True,
                                  "created_at": NOW.isoformat()}])
    monkeypatch.setattr(db_module, "get_notification_history", lambda limit=100: [])
    monkeypatch.setattr(db_module, "get_alert_rules", lambda enabled_only=False: [])
    monkeypatch.setattr(db_module, "get_report_settings",
                        lambda: {"id": 1, "enabled": False, "frequency": "daily",
                                 "hour": 8, "last_sent": None})
    monkeypatch.setattr(db_module, "ping", lambda: True)
    return bell_store


@pytest.fixture
def dead_db(monkeypatch):
    """Simulate Postgres being completely unreachable (no real connections)."""
    def _boom(*args, **kwargs):
        raise RuntimeError("database is down (test)")
    monkeypatch.setattr(db_module, "get_pool", _boom)
    return _boom


# ── Clients ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    """TestClient with authentication disabled (empty portal password)."""
    import main
    monkeypatch.setattr(settings, "portal_password", "")
    return TestClient(main.app, follow_redirects=False,
                      raise_server_exceptions=False)


@pytest.fixture
def audit_log(monkeypatch):
    """Capture audit-log writes instead of touching the DB."""
    records: list[tuple] = []
    monkeypatch.setattr(security, "record_audit",
                        lambda method, path, ip: records.append((method, path, ip)))
    return records


@pytest.fixture
def auth_client(monkeypatch, audit_log):
    """TestClient with a portal password configured (auth enforced)."""
    import main
    monkeypatch.setattr(settings, "portal_password", TEST_PASSWORD)
    monkeypatch.setattr(settings, "session_secret", TEST_SECRET)
    # No artificial brute-force delay in tests.
    monkeypatch.setattr(auth_routes, "FAILED_LOGIN_DELAY_SECONDS", 0)
    security.login_limiter._attempts.clear()
    c = TestClient(main.app, follow_redirects=False,
                   raise_server_exceptions=False)
    c.audit_log = audit_log
    return c


def login(test_client, password=TEST_PASSWORD, next_path="/"):
    """POST the login form; returns the response (cookie lands on the client)."""
    return test_client.post("/login", data={"password": password, "next": next_path})
