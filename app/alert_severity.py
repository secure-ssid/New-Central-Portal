"""One vocabulary for alert severity, and one normaliser for Central alerts.

Severity was mapped in four places that did not agree, and counted in three,
from two different fetch limits — so the dashboard stat card, the alerts hub
and the ticker could each report a different number for the same alerts. The
divergences were real, not theoretical: `_count_active_alerts` was missing the
warning/warn -> major branch that the other three had, so a "Warning" alert
counted as *other* on the dashboard and *major* everywhere else.

The Central payload was also being read with the wrong keys. On this tenant
GET /network-notifications/v1/alerts returns items keyed:

    action, category, clearedReason, createdAt, deferredUntil, deviceType, id,
    key, name, notes, priority, resolvedNotes, rootCause, severity, siteName,
    status, summary, type, updatedAt, updatedBy

There is no deviceName, serialNumber, description, message, alertName or
timeAt anywhere in it — all fields the old normaliser looked for. Every alert
therefore rendered with an empty body and an empty device column. Severity
values arrive capitalised (Critical / Major / Minor).

This module is deliberately pure: no db, no routes, no network. That is what
makes it testable against real payload fixtures rather than against a stub of
the layer the bug lives in.
"""
from __future__ import annotations

# Shared fetch size. The dashboard used 50 and the hub 100, which produced two
# different central_bridge cache entries and — on a tenant with more alerts than
# the smaller cap — two legitimately different totals for "active alerts".
ALERT_FETCH_LIMIT = 100

# Display order, worst first. These four are the buckets the UI already knows
# how to colour — the hub ladder, the notification bell's severity list and the
# ?severity= query whitelist all speak exactly this vocabulary.
SEVERITY_BUCKETS = ("critical", "major", "minor", "other")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITY_BUCKETS)}

_ALIASES = {
    "critical": "critical",
    "crit": "critical",
    "fatal": "critical",
    "emergency": "critical",
    "major": "major",
    "high": "major",
    # warning/warn were major in three mappers and "other" in the fourth.
    "warning": "major",
    "warn": "major",
    "error": "major",
    "minor": "minor",
    "medium": "minor",
    "low": "minor",
    "moderate": "minor",
}


def normalize_severity(value) -> str:
    """Map any severity spelling onto one of SEVERITY_BUCKETS.

    Idempotent: normalize_severity(normalize_severity(x)) == normalize_severity(x).
    """
    return _ALIASES.get(str(value or "").strip().lower(), "other")


def count_severities(alerts) -> dict:
    """Count alerts per bucket. Always returns total plus every bucket key.

    Accepts raw Central alerts or already-normalised dicts — normalize_severity
    is idempotent, so callers do not have to care which they hold.
    """
    summary = {"total": 0}
    summary.update({bucket: 0 for bucket in SEVERITY_BUCKETS})
    for alert in alerts or []:
        if not isinstance(alert, dict):
            continue
        summary["total"] += 1
        raw = alert.get("severity")
        if raw is None:
            raw = alert.get("alertSeverity")
        summary[normalize_severity(raw)] += 1
    return summary


def normalize_central_alert(raw: dict) -> dict:
    """Flatten a Central alert into the shape the templates render.

    Key choices are made against the live payload listed in the module
    docstring; the legacy spellings are kept as fallbacks so a payload change
    degrades to a blank field rather than a KeyError.
    """
    time_raw = raw.get("createdAt") or raw.get("timeAt") or raw.get("updatedAt") or raw.get("timestamp") or ""
    return {
        "source": "central",
        "id": raw.get("id") or raw.get("key") or raw.get("alertId") or "",
        "title": raw.get("name") or raw.get("title") or raw.get("alertName") or "Alert",
        # Central calls the human description "summary".
        "body": raw.get("summary") or raw.get("description") or raw.get("message") or "",
        "severity": normalize_severity(raw.get("severity") or raw.get("alertSeverity")),
        # Absent from this tenant's payload — Central scopes these alerts to a
        # device *type* rather than a named device — but read anyway, because
        # "not present here" is not the same as "never present", and dropping a
        # serial that did arrive would be a fresh bug.
        "device": raw.get("deviceName") or raw.get("device_name") or raw.get("serialNumber") or "",
        "device_serial": raw.get("serialNumber") or raw.get("serial") or raw.get("device_serial") or "",
        "device_type": raw.get("deviceType") or "",
        "category": raw.get("category") or "",
        "status": raw.get("status") or "",
        "priority": raw.get("priority") or "",
        "site": raw.get("siteName") or raw.get("site_name") or "",
        "time": time_raw,
    }
