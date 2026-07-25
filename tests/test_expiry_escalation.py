"""Expiry alerting: the escalation ladder and the dedup keys.

Two defects lived here undetected because nothing exercised these functions at
all — grep found no test calling check_subscriptions or check_ssl_certs.

1. Both loops scanned `sorted(thresholds, reverse=True)` and broke on the first
   match, taking the LOOSEST gate rather than the tightest. An item first seen
   at 85 days was bucketed at 90 and stayed there through 59, 29, 14 and past
   expiry, so it got exactly one email in its whole life.

2. The dedup keys carried no event identity. Subscriptions used the constant
   f"batch_{threshold}d", so one 90-day email per recipient for the lifetime of
   the database, for any subscription, ever. SSL used the bare hostname, so a
   host went silent forever after its first certificate.

The fake DB below reproduces the real UNIQUE(source_type, source_id, threshold,
recipient) constraint, because that constraint is what turns a bad key into
permanent silence. tests/test_alert_engine.py's fake has record_notification
but no was_notified, which is precisely why it could not catch this.
"""
from datetime import datetime, timedelta, timezone

import pytest

import notifications


class FakeDB:
    """Mimics the notifications_sent UNIQUE key and the settings table."""

    def __init__(self, thresholds="90,60,30,15", recipients=("ops@example.com",)):
        self.settings = {
            "thresholds": thresholds,
            "check_subscriptions": "true",
            "check_ssl": "true",
        }
        self._recipients = [{"email": e} for e in recipients]
        self.sent: set[tuple] = set()          # the UNIQUE key
        self.records: list[tuple] = []         # insertion order, for assertions

    def get_setting(self, key):
        return self.settings.get(key, "")

    def get_recipients(self):
        return list(self._recipients)

    def was_notified(self, source_type, source_id, threshold, recipient):
        return (source_type, source_id, threshold, recipient) in self.sent

    def record_notification(self, source_type, source_id, threshold, recipient, details=""):
        key = (source_type, source_id, threshold, recipient)
        if key in self.sent:            # ON CONFLICT DO NOTHING
            return
        self.sent.add(key)
        self.records.append(key)


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(notifications, "db", db)
    monkeypatch.setattr(notifications, "_send_email", lambda *a, **k: True)
    return db


def _sub(end: datetime, key="SUB-A", ident="id-a"):
    return {
        "id": ident, "key": key, "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quantity": "10", "availableQuantity": "2",   # in_use = 8
    }


# ── The bucket rule ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("days_left,expected", [
    (95, None),   # not due yet
    (90, 90), (85, 90), (61, 90),
    (60, 60), (59, 60), (31, 60),
    (30, 30), (29, 30), (16, 30),
    (15, 15), (14, 15), (0, 15), (-3, 15),   # already expired still alerts
])
def test_bucket_is_the_tightest_matching_gate(days_left, expected):
    assert notifications._bucket_for(days_left, [90, 60, 30, 15]) == expected


def test_bucket_ignores_threshold_ordering():
    for order in ([90, 60, 30, 15], [15, 30, 60, 90], [30, 90, 15, 60]):
        assert notifications._bucket_for(59, order) == 60


def test_bucket_with_no_thresholds_never_fires():
    assert notifications._bucket_for(1, []) is None


# ── Escalation, by advancing the clock ───────────────────────────────────────

def test_subscription_escalates_through_every_gate(fake_db):
    """The headline defect: one email for life, at the coarsest gate."""
    expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
    fired = []
    for days_out in (88, 61, 59, 31, 29, 16, 14, 2):
        now = expiry - timedelta(days=days_out)
        alerts = notifications.check_subscriptions(subs=[_sub(expiry)], now=now)
        for a in alerts:
            fired.append((days_out, a["threshold"]))

    # One email per gate, each at the first run that crosses it.
    assert fired == [(88, 90), (59, 60), (29, 30), (14, 15)]
    assert sorted(t for _, _, t, _ in fake_db.records) == [15, 30, 60, 90]


def test_ssl_cert_escalates_through_every_gate(fake_db, monkeypatch):
    not_after = datetime(2026, 12, 31, tzinfo=timezone.utc)
    fake_db.settings["ssl_hosts"] = "portal.example.com"
    monkeypatch.setattr(notifications, "_probe_cert", lambda h, p, t=10: not_after)

    fired = []
    for days_out in (88, 61, 59, 31, 29, 14):
        alerts = notifications.check_ssl_certs(now=not_after - timedelta(days=days_out))
        fired.extend(a["threshold"] for a in alerts)
    assert fired == [90, 60, 30, 15]


# ── Dedup keys identify the event, not the bucket ────────────────────────────

def test_a_second_subscription_still_alerts_at_a_gate_already_used(fake_db):
    """The old constant key meant the first sub to hit 90d silenced 90d for
    every future subscription, for every recipient, forever."""
    expiry_a = datetime(2027, 1, 31, tzinfo=timezone.utc)
    now = expiry_a - timedelta(days=88)
    assert notifications.check_subscriptions(subs=[_sub(expiry_a, "SUB-A", "id-a")], now=now)

    # A different subscription, same 90-day gate, same recipient.
    expiry_b = now + timedelta(days=88)
    alerts = notifications.check_subscriptions(
        subs=[_sub(expiry_b, "SUB-B", "id-b")], now=now
    )
    assert alerts, "a different subscription must still alert at the 90d gate"
    assert alerts[0]["threshold"] == 90


def test_renewing_a_subscription_starts_a_fresh_ladder(fake_db):
    """Extending the end date is a new expiry event."""
    original = datetime(2026, 9, 1, tzinfo=timezone.utc)
    now = original - timedelta(days=88)
    assert notifications.check_subscriptions(subs=[_sub(original)], now=now)
    # Same run again — suppressed.
    assert not notifications.check_subscriptions(subs=[_sub(original)], now=now)
    # Renewed a year out; when it comes back to 88 days it must alert again.
    renewed = original + timedelta(days=365)
    later = renewed - timedelta(days=88)
    assert notifications.check_subscriptions(subs=[_sub(renewed)], now=later)


def test_repeat_run_in_the_same_gate_stays_suppressed(fake_db):
    expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
    now = expiry - timedelta(days=88)
    assert notifications.check_subscriptions(subs=[_sub(expiry)], now=now)
    for _ in range(3):
        assert notifications.check_subscriptions(
            subs=[_sub(expiry)], now=now + timedelta(days=1)
        ) == []


def test_ssl_host_alerts_again_after_the_certificate_is_renewed(fake_db, monkeypatch):
    """Keyed on the bare hostname, a host was silent for good after one cert."""
    fake_db.settings["ssl_hosts"] = "portal.example.com"
    first = datetime(2026, 9, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(notifications, "_probe_cert", lambda h, p, t=10: first)
    assert notifications.check_ssl_certs(now=first - timedelta(days=88))

    renewed = first + timedelta(days=365)
    monkeypatch.setattr(notifications, "_probe_cert", lambda h, p, t=10: renewed)
    assert notifications.check_ssl_certs(now=renewed - timedelta(days=88)), \
        "a renewed certificate must alert again"


def test_two_ports_on_one_host_are_tracked_separately(fake_db, monkeypatch):
    """443 and 8443 shared a single dedup namespace, so one silenced the other."""
    fake_db.settings["ssl_hosts"] = "portal.example.com:443,portal.example.com:8443"
    not_after = datetime(2026, 12, 31, tzinfo=timezone.utc)
    monkeypatch.setattr(notifications, "_probe_cert", lambda h, p, t=10: not_after)

    alerts = notifications.check_ssl_certs(now=not_after - timedelta(days=88))
    assert len(alerts) == 2, "both endpoints must alert"
    assert len({a["id"] for a in alerts}) == 2, "and be deduped independently"


def test_each_recipient_is_tracked_separately(fake_db, monkeypatch):
    monkeypatch.setattr(
        fake_db, "_recipients",
        [{"email": "ops@example.com"}, {"email": "noc@example.com"}],
    )
    expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
    alerts = notifications.check_subscriptions(
        subs=[_sub(expiry)], now=expiry - timedelta(days=88)
    )
    assert {a["recipient"] for a in alerts} == {"ops@example.com", "noc@example.com"}


def test_source_type_matches_what_the_history_badge_expects(fake_db):
    """notifications.html colours the badge on source_type === 'subscription';
    the code used to write 'subscription_batch', so it never matched."""
    expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
    notifications.check_subscriptions(subs=[_sub(expiry)], now=expiry - timedelta(days=88))
    assert {r[0] for r in fake_db.records} == {"subscription"}


def test_nothing_is_recorded_when_the_email_fails(fake_db, monkeypatch):
    """Otherwise a bounced send permanently suppresses its own retry."""
    monkeypatch.setattr(notifications, "_send_email", lambda *a, **k: False)
    expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
    now = expiry - timedelta(days=88)
    alerts = notifications.check_subscriptions(subs=[_sub(expiry)], now=now)
    assert alerts and alerts[0]["sent"] is False
    assert fake_db.records == []
    # And the retry on the next run is not suppressed.
    assert notifications.check_subscriptions(subs=[_sub(expiry)], now=now)


def test_unused_subscriptions_are_ignored(fake_db):
    expiry = datetime(2026, 12, 31, tzinfo=timezone.utc)
    unused = _sub(expiry)
    unused["availableQuantity"] = unused["quantity"]      # in_use == 0
    assert notifications.check_subscriptions(
        subs=[unused], now=expiry - timedelta(days=10)
    ) == []
