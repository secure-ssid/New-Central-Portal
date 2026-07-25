"""One severity vocabulary, one counter, one Central normaliser.

Severity was mapped in four places that disagreed and counted in three, from
two different fetch limits. The divergence was concrete: `_count_active_alerts`
lacked the warning/warn -> major branch the other three had, so a "Warning"
alert was *other* on the dashboard and *major* on the hub.

The payload fixtures below are the real keys and casing observed on the live
tenant (GET /network-notifications/v1/alerts), which is the point — the old
normaliser was written against keys Central never sends.
"""
import pytest

from alert_severity import (
    ALERT_FETCH_LIMIT,
    SEVERITY_BUCKETS,
    count_severities,
    normalize_central_alert,
    normalize_severity,
)

# Trimmed from a live alert. Note: capitalised severity, "name" not
# "alertName", "summary" not "description", "createdAt" not "timeAt", and no
# deviceName/serialNumber key at all.
LIVE_ALERT = {
    "id": "b4f1c0de-0000-4a1b-9f11-1c2d3e4f5a6b",
    "key": "AP_DOWN",
    "name": "Access point down",
    "summary": "AP735-LR has been unreachable for 12 minutes",
    "severity": "Critical",
    "status": "Active",
    "category": "Availability",
    "priority": "P1",
    "deviceType": "ACCESS_POINT",
    "siteName": "SecureSSID",
    "createdAt": "2026-07-25T10:00:00Z",
    "rootCause": "Device stopped responding",
}


@pytest.mark.parametrize("raw,expected", [
    ("Critical", "critical"), ("critical", "critical"), ("CRIT", "critical"),
    ("fatal", "critical"), ("emergency", "critical"),
    ("Major", "major"), ("high", "major"), ("error", "major"),
    ("Minor", "minor"), ("medium", "minor"), ("low", "minor"), ("moderate", "minor"),
    ("", "other"), (None, "other"), ("nonsense", "other"), ("  Major  ", "major"),
])
def test_severity_aliases(raw, expected):
    assert normalize_severity(raw) == expected


def test_warning_is_major_everywhere():
    """The exact divergence: dashboard said 'other', hub said 'major'."""
    assert normalize_severity("Warning") == "major"
    assert normalize_severity("warn") == "major"


def test_normalize_is_idempotent():
    """Callers must not care whether they hold raw or already-mapped alerts."""
    for raw in ("Critical", "warn", "bogus", None):
        once = normalize_severity(raw)
        assert normalize_severity(once) == once


def test_counts_sum_to_total():
    alerts = [{"severity": s} for s in
              ("Critical", "Major", "Warning", "Minor", "low", "bogus", None)]
    summary = count_severities(alerts)
    assert summary["total"] == 7
    assert sum(summary[b] for b in SEVERITY_BUCKETS) == summary["total"]
    assert summary == {"total": 7, "critical": 1, "major": 2, "minor": 2, "other": 2}


def test_counter_always_returns_every_bucket():
    """Templates index these keys directly; a missing one is a 500."""
    summary = count_severities([])
    assert summary["total"] == 0
    for bucket in SEVERITY_BUCKETS:
        assert summary[bucket] == 0


def test_counter_tolerates_junk_entries():
    assert count_severities([None, "oops", 42, {"severity": "Major"}])["total"] == 1


def test_dashboard_and_hub_agree_on_the_same_input():
    """The whole point. Both pages now go through count_severities."""
    from routes.home import _count_active_alerts

    alerts = [{"severity": s} for s in ("Critical", "Warning", "High", "Minor", "bogus")]
    assert _count_active_alerts(alerts) == count_severities(alerts)


def test_counting_normalised_alerts_matches_counting_raw_ones():
    """The hub counts normalised dicts; the dashboard counts raw ones."""
    raw = [{"severity": s} for s in ("Critical", "Warning", "Minor", "bogus")]
    normalised = [normalize_central_alert(a) for a in raw]
    assert count_severities(normalised) == count_severities(raw)


# ── Central payload mapping ──────────────────────────────────────────────────

def test_reads_the_keys_central_actually_sends():
    alert = normalize_central_alert(LIVE_ALERT)
    assert alert["title"] == "Access point down"          # name, not alertName
    assert alert["body"].startswith("AP735-LR")           # summary, not description
    assert alert["time"] == "2026-07-25T10:00:00Z"        # createdAt, not timeAt
    assert alert["severity"] == "critical"                # capitalised input
    assert alert["site"] == "SecureSSID"
    assert alert["device_type"] == "ACCESS_POINT"
    assert alert["category"] == "Availability"


def test_body_is_not_blank_for_a_real_alert():
    """It was, for every alert: the old code read description/message."""
    assert normalize_central_alert(LIVE_ALERT)["body"] != ""


def test_legacy_payload_keys_still_resolve():
    """A serial that does arrive must not be dropped."""
    legacy = {
        "alertName": "AP Down",
        "severity": "critical",
        "serialNumber": "AP1SERIAL",
        "deviceName": "lobby-ap",
        "timeAt": "2026-07-04T10:00:00Z",
        "description": "legacy body",
    }
    alert = normalize_central_alert(legacy)
    assert alert["title"] == "AP Down"
    assert alert["device_serial"] == "AP1SERIAL"
    assert alert["device"] == "lobby-ap"
    assert alert["body"] == "legacy body"
    assert alert["time"] == "2026-07-04T10:00:00Z"


def test_unknown_payload_degrades_instead_of_raising():
    alert = normalize_central_alert({})
    assert alert["title"] == "Alert"
    assert alert["severity"] == "other"
    assert alert["body"] == ""


def test_both_pages_use_one_fetch_limit():
    """50 on the dashboard vs 100 on the hub meant two cache entries and, on a
    busy tenant, two legitimately different totals."""
    from routes.home import ALERT_SUMMARY_LIMIT

    assert ALERT_SUMMARY_LIMIT == ALERT_FETCH_LIMIT


def test_severity_query_whitelist_matches_the_vocabulary():
    """/alerts/?severity=x is validated against a hardcoded tuple; if the
    vocabulary grows and that list does not, the filter silently ignores it."""
    import inspect

    import routes.alerts as alerts_route

    source = inspect.getsource(alerts_route.alerts_hub)
    for bucket in SEVERITY_BUCKETS:
        assert f'"{bucket}"' in source, f"{bucket} missing from the ?severity= whitelist"
