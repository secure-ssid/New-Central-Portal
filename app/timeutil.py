"""Small time-formatting helpers shared across routes.

Pure and deterministic (an injectable ``now`` keeps tests stable). Kept out of
timeseries.py, which is about chart-series maths, so a route needing a human
"3 weeks ago" label does not pull in that module's surface.
"""
from __future__ import annotations

from datetime import datetime, timezone


def relative_age(epoch_seconds: float | None, now: datetime | None = None) -> str | None:
    """A short human age like "3 weeks ago" for a past epoch, or None.

    None for a missing input or a future timestamp (a "-2 days ago" reads as a
    bug, so a clock skew renders nothing rather than something wrong).
    """
    if epoch_seconds is None:
        return None
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    delta = now_ts - epoch_seconds
    if delta < 0:
        return None
    if delta < 3600:
        return "just now" if delta < 60 else f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    days = int(delta // 86400)
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    if days < 365:
        return f"{days // 30} month{'s' if days // 30 != 1 else ''} ago"
    return f"{days // 365} year{'s' if days // 365 != 1 else ''} ago"


def relative_age_of(value, now: datetime | None = None) -> str | None:
    """relative_age for an ISO-8601 string or an epoch value (s or ms)."""
    from timeseries import to_epoch_seconds
    return relative_age(to_epoch_seconds(value), now=now)
