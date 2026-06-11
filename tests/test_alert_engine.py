"""Device-down alert engine + scheduled summary report (injectable, no DB)."""
from datetime import datetime, timedelta, timezone

import pytest

import db as db_module
import notifications as notif

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def dev(serial, status="online", name=None, site="HQ", dtype="ap"):
    return {"serial": serial, "name": name or serial.lower(), "site": site,
            "type": dtype, "status": status}


class FakeAlertDB:
    """In-memory stand-in for the db helpers the engine touches."""

    def __init__(self):
        self.rules = [{"id": 1, "enabled": True, "site_filter": None,
                       "device_type_filter": None, "offline_minutes": 5,
                       "cooldown_minutes": 60}]
        self.recipients = [{"email": "ops@example.com"}]
        self.snapshot: dict[str, dict] = {}
        self.in_app: list[dict] = []
        self.transitions: list[tuple] = []
        self.notifications_sent: list[tuple] = []
        self.down = False  # flip to simulate a dead database

    def _check(self):
        if self.down:
            raise RuntimeError("database down (test)")

    # db API surface -----------------------------------------------------
    def get_alert_rules(self, enabled_only=False):
        self._check()
        return [dict(r) for r in self.rules
                if not enabled_only or r.get("enabled", True)]

    def get_recipients(self):
        self._check()
        return list(self.recipients)

    def load_device_snapshot(self):
        self._check()
        return {s: dict(r) for s, r in self.snapshot.items()}

    def upsert_device_snapshot(self, serial, name, status,
                               offline_since=None, alerted_at=None):
        self._check()
        self.snapshot[serial] = {"serial": serial, "name": name, "status": status,
                                 "offline_since": offline_since,
                                 "alerted_at": alerted_at}

    def record_status_transition(self, serial, name, status):
        self._check()
        self.transitions.append((serial, status))

    def add_in_app_notification(self, title, body="", severity="info",
                                device_serial=None, url=None):
        self._check()
        self.in_app.append({"title": title, "body": body, "severity": severity,
                            "device_serial": device_serial, "url": url})

    def record_notification(self, source_type, source_id, threshold,
                            recipient, details=""):
        self._check()
        self.notifications_sent.append((source_type, source_id, recipient))


@pytest.fixture
def engine(monkeypatch):
    """Fresh engine state wired to a FakeAlertDB and a captured email outbox."""
    fake = FakeAlertDB()
    notif._reset_engine_state_for_tests()
    for name in ("get_alert_rules", "get_recipients", "load_device_snapshot",
                 "upsert_device_snapshot", "record_status_transition",
                 "add_in_app_notification", "record_notification"):
        monkeypatch.setattr(db_module, name, getattr(fake, name))
    outbox: list[tuple] = []
    monkeypatch.setattr(notif, "_send_email",
                        lambda to, subject, html: outbox.append((to, subject)) or True)
    fake.outbox = outbox
    yield fake
    notif._reset_engine_state_for_tests()


run = notif.run_device_status_check


class TestBaseline:
    def test_first_run_seeds_without_alerting(self, engine):
        events = run(devices=[dev("A1"), dev("B2", status="offline")], now=T0)
        assert events == []
        assert engine.in_app == []
        assert engine.outbox == []
        # Snapshot persisted for both devices.
        assert set(engine.snapshot) == {"A1", "B2"}

    def test_baseline_offline_device_never_alerts_for_that_outage(self, engine):
        run(devices=[dev("B2", status="offline")], now=T0)
        events = run(devices=[dev("B2", status="offline")], now=at(120))
        assert events == []
        assert engine.outbox == []

    def test_new_device_mid_flight_baselined_quietly(self, engine):
        run(devices=[dev("A1")], now=T0)
        events = run(devices=[dev("A1"), dev("NEW", status="offline")], now=at(1))
        assert events == []
        # Even long past the threshold: discovered-offline device has no timer
        # start older than discovery.
        events = run(devices=[dev("A1"), dev("NEW", status="offline")], now=at(2))
        assert events == []

    def test_devices_none_and_fetch_failure_aborts(self, engine, monkeypatch):
        monkeypatch.setattr(notif, "_fetch_devices_sync", lambda: None)
        assert run(devices=None, now=T0) == []
        assert not notif._state_seeded


class TestDownAlerts:
    def test_threshold_alert_fires_once(self, engine):
        run(devices=[dev("A1")], now=T0)

        events = run(devices=[dev("A1", status="offline")], now=at(1))
        assert events == [{"serial": "A1", "event": "went_offline"}]
        assert ("A1", "offline") in engine.transitions

        # Below the 5-minute threshold: nothing yet.
        assert run(devices=[dev("A1", status="offline")], now=at(4)) == []
        assert engine.outbox == []

        # Threshold crossed: exactly one alert.
        events = run(devices=[dev("A1", status="offline")], now=at(7))
        assert events == [{"serial": "A1", "event": "down_alert", "rule_id": 1}]
        assert len(engine.outbox) == 1
        assert engine.outbox[0][0] == "ops@example.com"
        assert len(engine.in_app) == 1
        assert engine.in_app[0]["severity"] == "critical"
        assert engine.in_app[0]["device_serial"] == "A1"
        assert engine.in_app[0]["url"] == "/devices/A1"

        # Still down: no duplicate alert for the same outage.
        assert run(devices=[dev("A1", status="offline")], now=at(30)) == []
        assert len(engine.outbox) == 1

    def test_recovery_notice_only_after_alert(self, engine):
        run(devices=[dev("A1")], now=T0)
        run(devices=[dev("A1", status="offline")], now=at(1))
        run(devices=[dev("A1", status="offline")], now=at(7))  # alert fired

        events = run(devices=[dev("A1")], now=at(10))
        assert events == [{"serial": "A1", "event": "recovered"}]
        infos = [n for n in engine.in_app if n["severity"] == "info"]
        assert len(infos) == 1
        assert "back online" in infos[0]["body"]

    def test_short_blip_recovers_silently(self, engine):
        run(devices=[dev("A1")], now=T0)
        run(devices=[dev("A1", status="offline")], now=at(1))
        events = run(devices=[dev("A1")], now=at(3))  # back before threshold
        assert events == []
        assert engine.in_app == []
        assert engine.outbox == []

    def test_cooldown_suppresses_next_outage_alert(self, engine):
        run(devices=[dev("A1")], now=T0)
        run(devices=[dev("A1", status="offline")], now=at(1))
        run(devices=[dev("A1", status="offline")], now=at(7))   # alert @7
        run(devices=[dev("A1")], now=at(10))                     # recovery
        run(devices=[dev("A1", status="offline")], now=at(11))  # down again

        # 6+ minutes offline but still inside the 60m cooldown from @7.
        assert run(devices=[dev("A1", status="offline")], now=at(20)) == []
        assert len(engine.outbox) == 1

        # Cooldown elapsed -> second alert allowed.
        events = run(devices=[dev("A1", status="offline")], now=at(70))
        assert events == [{"serial": "A1", "event": "down_alert", "rule_id": 1}]
        assert len(engine.outbox) == 2


class TestRuleFilters:
    def test_site_filter_mismatch_never_alerts(self, engine):
        engine.rules = [{"id": 1, "enabled": True, "site_filter": "Branch",
                         "device_type_filter": None, "offline_minutes": 5,
                         "cooldown_minutes": 60}]
        run(devices=[dev("A1", site="HQ")], now=T0)
        run(devices=[dev("A1", status="offline", site="HQ")], now=at(1))
        assert run(devices=[dev("A1", status="offline", site="HQ")], now=at(30)) == []
        assert engine.outbox == []

    def test_site_filter_match_is_case_insensitive(self, engine):
        engine.rules = [{"id": 1, "enabled": True, "site_filter": "branch",
                         "device_type_filter": None, "offline_minutes": 5,
                         "cooldown_minutes": 60}]
        run(devices=[dev("A1", site="Branch")], now=T0)
        run(devices=[dev("A1", status="offline", site="Branch")], now=at(1))
        events = run(devices=[dev("A1", status="offline", site="Branch")], now=at(7))
        assert [e["event"] for e in events] == ["down_alert"]

    def test_device_type_filter(self, engine):
        engine.rules = [{"id": 1, "enabled": True, "site_filter": None,
                         "device_type_filter": "switch", "offline_minutes": 5,
                         "cooldown_minutes": 60}]
        run(devices=[dev("AP1", dtype="ap"), dev("SW1", dtype="switch")], now=T0)
        run(devices=[dev("AP1", dtype="ap", status="offline"),
                     dev("SW1", dtype="switch", status="offline")], now=at(1))
        events = run(devices=[dev("AP1", dtype="ap", status="offline"),
                              dev("SW1", dtype="switch", status="offline")], now=at(7))
        assert [(e["serial"], e["event"]) for e in events] == [("SW1", "down_alert")]

    def test_type_filter_normalises_aliases(self, engine):
        engine.rules = [{"id": 1, "enabled": True, "site_filter": None,
                         "device_type_filter": "ap", "offline_minutes": 5,
                         "cooldown_minutes": 60}]
        run(devices=[dev("AP1", dtype="access_point")], now=T0)
        run(devices=[dev("AP1", dtype="access_point", status="offline")], now=at(1))
        events = run(devices=[dev("AP1", dtype="access_point", status="offline")],
                     now=at(7))
        assert [e["event"] for e in events] == ["down_alert"]

    def test_disabled_rule_means_no_alerts(self, engine):
        engine.rules = [{"id": 1, "enabled": False, "site_filter": None,
                         "device_type_filter": None, "offline_minutes": 5,
                         "cooldown_minutes": 60}]
        run(devices=[dev("A1")], now=T0)
        run(devices=[dev("A1", status="offline")], now=at(1))
        assert run(devices=[dev("A1", status="offline")], now=at(30)) == []
        assert engine.outbox == []

    def test_most_aggressive_matching_rule_wins(self, engine):
        engine.rules = [
            {"id": 1, "enabled": True, "site_filter": None,
             "device_type_filter": None, "offline_minutes": 30,
             "cooldown_minutes": 60},
            {"id": 2, "enabled": True, "site_filter": None,
             "device_type_filter": None, "offline_minutes": 5,
             "cooldown_minutes": 60},
        ]
        run(devices=[dev("A1")], now=T0)
        run(devices=[dev("A1", status="offline")], now=at(1))
        events = run(devices=[dev("A1", status="offline")], now=at(7))
        assert events == [{"serial": "A1", "event": "down_alert", "rule_id": 2}]


class TestRestartRestore:
    def test_already_alerted_outage_not_realerted_after_restart(self, engine):
        engine.snapshot = {"A1": {"serial": "A1", "name": "a1", "status": "offline",
                                  "offline_since": at(-30), "alerted_at": at(-20)}}
        run(devices=[dev("A1", status="offline")], now=T0)   # seed from snapshot
        events = run(devices=[dev("A1", status="offline")], now=at(5))
        assert events == []
        assert engine.outbox == []

    def test_pending_down_timer_survives_restart(self, engine):
        engine.snapshot = {"A1": {"serial": "A1", "name": "a1", "status": "offline",
                                  "offline_since": at(-10), "alerted_at": None}}
        run(devices=[dev("A1", status="offline")], now=T0)   # seed: timer kept
        events = run(devices=[dev("A1", status="offline")], now=at(1))
        assert [e["event"] for e in events] == ["down_alert"]

    def test_went_offline_while_app_was_down_starts_fresh_timer(self, engine):
        engine.snapshot = {"A1": {"serial": "A1", "name": "a1", "status": "online",
                                  "offline_since": None, "alerted_at": None}}
        run(devices=[dev("A1", status="offline")], now=T0)   # seed: timer = now
        assert run(devices=[dev("A1", status="offline")], now=at(4)) == []
        events = run(devices=[dev("A1", status="offline")], now=at(6))
        assert [e["event"] for e in events] == ["down_alert"]

    def test_recovery_after_restart_for_alerted_outage(self, engine):
        engine.snapshot = {"A1": {"serial": "A1", "name": "a1", "status": "offline",
                                  "offline_since": at(-30), "alerted_at": at(-20)}}
        run(devices=[dev("A1", status="offline")], now=T0)
        events = run(devices=[dev("A1")], now=at(2))
        assert [e["event"] for e in events] == ["recovered"]


class TestDbDownResilience:
    def test_engine_keeps_alerting_with_fallback_rule(self, engine):
        engine.down = True
        assert run(devices=[dev("A1")], now=T0) == []        # seeds despite errors
        events = run(devices=[dev("A1", status="offline")], now=at(1))
        assert events == [{"serial": "A1", "event": "went_offline"}]
        events = run(devices=[dev("A1", status="offline")], now=at(7))
        # Fallback rule (id 0) drives the alert; in-app/email writes fail soft.
        assert events == [{"serial": "A1", "event": "down_alert", "rule_id": 0}]

    def test_cached_rules_used_when_db_dies_later(self, engine):
        engine.rules = [{"id": 9, "enabled": True, "site_filter": None,
                         "device_type_filter": None, "offline_minutes": 5,
                         "cooldown_minutes": 60}]
        run(devices=[dev("A1")], now=T0)
        run(devices=[dev("A1", status="offline")], now=at(1))  # rules cached here
        engine.down = True
        events = run(devices=[dev("A1", status="offline")], now=at(7))
        assert events == [{"serial": "A1", "event": "down_alert", "rule_id": 9}]


# ── Scheduled summary report ─────────────────────────────────────────────────

class FakeReportDB:
    def __init__(self):
        self.cfg = {"id": 1, "enabled": True, "frequency": "daily", "hour": 12,
                    "last_sent": None}
        self.recipients = [{"email": "ops@example.com"}]
        self.sent_records: list[tuple] = []
        self.last_sent_marked = None
        self.down = False

    def get_report_settings(self):
        if self.down:
            raise RuntimeError("db down")
        return dict(self.cfg)

    def get_recipients(self):
        return list(self.recipients)

    def record_notification(self, source_type, source_id, threshold,
                            recipient, details=""):
        self.sent_records.append((source_type, recipient))

    def mark_report_sent(self, when=None):
        self.last_sent_marked = when

    def count_recent_alerts(self, hours=24):
        return 3


@pytest.fixture
def report_db(monkeypatch):
    fake = FakeReportDB()
    for name in ("get_report_settings", "get_recipients", "record_notification",
                 "mark_report_sent", "count_recent_alerts"):
        monkeypatch.setattr(db_module, name, getattr(fake, name))
    outbox = []
    monkeypatch.setattr(notif, "_send_email",
                        lambda to, subject, html: outbox.append((to, subject)) or True)
    fake.outbox = outbox
    return fake


DEVICES = [dev("SW1", dtype="switch"), dev("AP1", status="offline")]


class TestSummaryReport:
    def test_sends_when_due(self, report_db):
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result == {"ok": True, "sent": 1, "recipients": 1}
        assert report_db.outbox[0][0] == "ops@example.com"
        assert report_db.last_sent_marked == T0
        assert ("summary_report", "ops@example.com") in report_db.sent_records

    def test_disabled_skips(self, report_db):
        report_db.cfg["enabled"] = False
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result == {"ok": True, "skipped": "disabled"}
        assert report_db.outbox == []

    def test_hour_mismatch_skips(self, report_db):
        report_db.cfg["hour"] = 7  # T0 is 12:00 UTC
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result["skipped"] == "hour mismatch"

    def test_weekly_only_fires_on_monday(self, report_db):
        report_db.cfg["frequency"] = "weekly"
        # T0 (2026-06-01) is a Monday; shift to Tuesday.
        tuesday = T0 + timedelta(days=1)
        assert notif.run_summary_report(devices=DEVICES, subs=[],
                                        now=tuesday)["skipped"] == "weekday"
        assert notif.run_summary_report(devices=DEVICES, subs=[],
                                        now=T0)["ok"] is True
        assert len(report_db.outbox) == 1

    def test_already_sent_in_window_skips(self, report_db):
        report_db.cfg["last_sent"] = T0 - timedelta(hours=2)
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result["skipped"] == "already sent in window"

    def test_naive_last_sent_treated_as_utc(self, report_db):
        report_db.cfg["last_sent"] = (T0 - timedelta(hours=2)).replace(tzinfo=None)
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result["skipped"] == "already sent in window"

    def test_force_bypasses_schedule(self, report_db):
        report_db.cfg["enabled"] = False
        report_db.cfg["hour"] = 3
        result = notif.run_summary_report(force=True, devices=DEVICES, subs=[],
                                          now=T0)
        assert result["ok"] is True and result["sent"] == 1

    def test_db_down_reports_error(self, report_db):
        report_db.down = True
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result == {"ok": False, "error": "Database unavailable"}

    def test_no_recipients_is_an_error(self, report_db):
        report_db.recipients = []
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result["ok"] is False

    def test_smtp_failure_reported(self, report_db, monkeypatch):
        monkeypatch.setattr(notif, "_send_email", lambda *a, **k: False)
        result = notif.run_summary_report(devices=DEVICES, subs=[], now=T0)
        assert result["ok"] is False and result["sent"] == 0
        assert report_db.last_sent_marked is None
