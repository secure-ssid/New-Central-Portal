"""One vocabulary for Aruba's DPI application-risk classification.

This is the application-visibility counterpart to ``app/alert_severity.py`` and
deliberately copies its shape — a closed bucket list, an alias table, an
idempotent normaliser and a counter — rather than importing it. The two
vocabularies are disjoint: Central grades an *alert* Critical/Major/Minor and an
*application* TRUSTWORTHY/LOW/MODERATE/SUSPICIOUS/NOT_EVALUATED. Folding one
into the other would have to invent a mapping that Central does not publish.

The payload this reads, from GET /network-monitoring/v1/applications, verified
against the live tenant:

    name, id, risk, state, rxBytes, txBytes, categories, applicationHostType,
    destLocation, experience, lastUsedTime, tlsVersion, certificateExpiryDate

Three of those are dead on this tenant and no caller should reach for them:
``experience`` is all-zero across every row of a 7-day window, ``tlsVersion``
and ``certificateExpiryDate`` are empty strings on every row, and ``state`` is
the constant "ALLOWED" (nothing here enforces an application policy).

Two properties of the live data drive the ranking helpers below:

1. ``risk`` is DPI-vendor-driven, not tenant-aware, so the largest talkers it
   flags are routinely benign — githubcopilot.com is both the biggest transmit
   talker on this network and classified SUSPICIOUS. A bytes-sorted watchlist
   therefore buries its own signal under false alarms.
2. The genuinely unexplained destinations are the ones Aruba could not
   categorise at all, and they are *small* — hundreds of kilobytes. Hence
   :func:`watchlist` surfaces unclassified-and-flagged as its own group above
   the bytes-sorted remainder, instead of one list sorted by size.

Pure module: no db, no routes, no network — so it is testable against real
payload fixtures rather than against a stub of the layer a bug would live in.
"""
from __future__ import annotations

from typing import Any

from timeseries import to_epoch_seconds

# Display order, most concerning first. "unknown" is last rather than second
# because NOT_EVALUATED means Aruba never formed an opinion, which is not
# evidence of risk — it is absence of evidence, and ranking it alongside
# SUSPICIOUS would swamp the strip on a tenant where 30 of 344 rows are
# unevaluated.
RISK_BUCKETS = ("suspicious", "moderate", "low", "trustworthy", "unknown")
RISK_RANK = {name: index for index, name in enumerate(RISK_BUCKETS)}

# Badge modifiers from app/static/app.css. Trustworthy gets no modifier: a page
# where the majority of rows are green reads as an alarm board with a stuck
# needle, and "normal" should be the quiet default.
RISK_TONE = {
    "suspicious": "badge--crit",
    "moderate": "badge--warn",
    "low": "badge--info",
    "trustworthy": "badge--ok",
    "unknown": "",
}

RISK_LABELS = {
    "suspicious": "Suspicious",
    "moderate": "Moderate",
    "low": "Low",
    "trustworthy": "Trustworthy",
    "unknown": "Not evaluated",
}

_ALIASES = {
    "suspicious": "suspicious",
    "high": "suspicious",
    "very_high": "suspicious",
    "moderate": "moderate",
    "medium": "moderate",
    "low": "low",
    "very_low": "low",
    "trustworthy": "trustworthy",
    "trusted": "trustworthy",
    "safe": "trustworthy",
    "not_evaluated": "unknown",
    "unevaluated": "unknown",
    "unknown": "unknown",
}

# The risk levels worth a second look. Everything else is noise on a board whose
# whole job is to be short enough to read.
WATCHED_RISKS = ("suspicious", "moderate")

# Category strings Aruba emits when its DPI could not place the destination.
_UNCLASSIFIED = {"", "not available", "not_available", "unknown", "n/a", "none"}


def normalize_risk(value: Any) -> str:
    """Map any risk spelling onto one of RISK_BUCKETS.

    Idempotent: normalize_risk(normalize_risk(x)) == normalize_risk(x).
    """
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _ALIASES.get(text, "unknown")


def risk_label(bucket: str) -> str:
    return RISK_LABELS.get(bucket, "Not evaluated")


def risk_tone(bucket: str) -> str:
    return RISK_TONE.get(bucket, "")


def _int(value: Any) -> int:
    """Bytes, or 0. Central sends these as ints today but has stringified
    numbers elsewhere in the same API (lastUsedTime), so do not assume."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return 0


def _categories(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [c.strip() for c in raw if isinstance(c, str) and c.strip()]


def _countries(raw: Any) -> list[str]:
    """Destination country names, de-duplicated, order preserved."""
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("countryName") or entry.get("countryCode") or ""
        name = str(name).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def normalize_app(raw: Any) -> dict | None:
    """Flatten one application record into the shape the template renders.

    Returns None for anything that is not a usable record, so callers can build
    a list with a single comprehension and never carry a half-record.
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    rx, tx = _int(raw.get("rxBytes")), _int(raw.get("txBytes"))
    categories = _categories(raw.get("categories"))
    bucket = normalize_risk(raw.get("risk"))
    return {
        "id": str(raw.get("id") or ""),
        "name": name,
        "risk": bucket,
        "risk_label": risk_label(bucket),
        "risk_tone": risk_tone(bucket),
        "rx": rx,
        "tx": tx,
        "total": rx + tx,
        "categories": categories,
        "unclassified": not any(
            c.lower() not in _UNCLASSIFIED for c in categories
        ),
        "countries": _countries(raw.get("destLocation")),
        "host_type": str(raw.get("applicationHostType") or "").strip(),
        # A string of epoch MILLIseconds on this API — not ISO, not a number.
        "last_used": to_epoch_seconds(raw.get("lastUsedTime")),
    }


def normalize_apps(rows: Any) -> list[dict]:
    """Every usable record from a raw list, largest talker first."""
    apps = [a for a in (normalize_app(r) for r in (rows or [])) if a]
    apps.sort(key=lambda a: -a["total"])
    return apps


def count_risks(apps: Any) -> dict:
    """Counts per bucket. Always returns total plus every bucket key."""
    summary = {"total": 0}
    summary.update({bucket: 0 for bucket in RISK_BUCKETS})
    for app in apps or []:
        if not isinstance(app, dict):
            continue
        summary["total"] += 1
        summary[normalize_risk(app.get("risk"))] += 1
    return summary


def risk_strip(apps: Any) -> list[dict]:
    """The posture strip: one entry per bucket, worst first, zeroes included.

    Zero-count buckets stay in so the strip has a stable width and a bucket
    dropping to zero is visible as a zero rather than as a missing chip.
    """
    counts = count_risks(apps)
    return [
        {"bucket": bucket, "label": risk_label(bucket),
         "tone": risk_tone(bucket), "count": counts[bucket]}
        for bucket in RISK_BUCKETS
    ]


def watchlist(apps: Any) -> tuple[list[dict], list[dict]]:
    """(unclassified-and-flagged, the bytes-sorted remainder).

    Split rather than sorted, because the two groups answer different
    questions. The first is "Aruba does not know what this is and does not like
    it" — a handful of rows, usually tiny, and the only genuinely actionable
    thing on the page. The second is "Aruba does not like this but does know
    what it is", which on any real network is mostly false alarms you learn to
    ignore.
    """
    flagged = [a for a in (apps or [])
               if isinstance(a, dict) and a.get("risk") in WATCHED_RISKS]
    unknown = [a for a in flagged if a.get("unclassified")]
    known = [a for a in flagged if not a.get("unclassified")]
    unknown.sort(key=lambda a: (RISK_RANK.get(a["risk"], 9), -a["total"]))
    known.sort(key=lambda a: -a["total"])
    return unknown, known


def top_talkers(apps: Any, limit: int = 25) -> list[dict]:
    rows = [a for a in (apps or []) if isinstance(a, dict)]
    rows.sort(key=lambda a: -a.get("total", 0))
    return rows[:limit]


def category_rollup(apps: Any, limit: int = 12) -> list[dict]:
    """Bytes grouped by category, largest first.

    ``categories`` is multi-valued, so an application contributes its full byte
    count to every category it carries and the parts sum to more than the whole.
    That is why this returns absolute totals and the template shares each bar
    against the largest category rather than against a grand total — a
    percent-of-total here would be arithmetic nonsense.
    """
    totals: dict[str, dict] = {}
    for app in apps or []:
        if not isinstance(app, dict):
            continue
        for category in app.get("categories") or []:
            entry = totals.setdefault(category, {"name": category, "total": 0, "apps": 0})
            entry["total"] += app.get("total", 0)
            entry["apps"] += 1
    rows = sorted(totals.values(), key=lambda r: -r["total"])
    return rows[:limit]
