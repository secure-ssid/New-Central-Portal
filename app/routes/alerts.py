"""Unified Alerts hub — Central active alerts + portal notification history."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from alert_severity import (
    ALERT_FETCH_LIMIT,
    count_severities,
    normalize_central_alert,
    normalize_severity,
)
from pagination import filter_items, paginate as _paginate

import db
from templates_shared import templates

router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _time_ago(value) -> str:
    ts = _parse_time(value)
    if ts is None:
        return ""
    secs = max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


# Severity vocabulary and the Central payload mapping now live in
# alert_severity so the dashboard, this hub and the ticker cannot disagree.
_severity_class = normalize_severity


def _normalize_central_alert(raw: dict) -> dict:
    alert = normalize_central_alert(raw)
    alert["time_ago"] = _time_ago(alert["time"])
    return alert


def _normalize_portal_alert(raw: dict) -> dict:
    created = raw.get("created_at") or raw.get("sent_at")
    time_str = ""
    if isinstance(created, datetime):
        time_str = created.astimezone(timezone.utc).isoformat()
    elif created:
        time_str = str(created)
    return {
        "source": "portal",
        "id": raw.get("id"),
        "title": raw.get("subject") or raw.get("title") or "Portal notification",
        "body": raw.get("body") or raw.get("message") or "",
        "severity": _severity_class(str(raw.get("severity") or "info")),
        "device": raw.get("device_serial") or "",
        "device_serial": raw.get("device_serial") or "",
        "site": "",
        "time": time_str,
        "time_ago": _time_ago(created),
    }


async def _load_alerts_context(
    severity_filter: str | None = None,
    q: str = "",
    request: Request | None = None,
) -> dict:
    central_alerts: list[dict] = []
    portal_history: list[dict] = []
    summary = {"total": 0, "critical": 0, "major": 0, "minor": 0, "other": 0}
    filtered: list[dict] = []
    pg = None

    try:
        from vendors.central_bridge import list_active_alerts
        raw = await list_active_alerts(limit=ALERT_FETCH_LIMIT)
        all_central = [_normalize_central_alert(a) for a in raw if isinstance(a, dict)]
        summary = count_severities(all_central)
        filtered = all_central
        if severity_filter:
            filtered = [a for a in filtered if a.get("severity") == severity_filter]
        if q:
            filtered = filter_items(filtered, q, "title", "body", "device", "device_serial", "site")
        pg = _paginate(request, filtered) if request is not None else None
        central_alerts = pg["items"] if pg else filtered
    except Exception as exc:
        logger.warning("Central alerts unavailable: %s", exc)

    try:
        # This page polls itself every 60s, so keep the blocking psycopg2 read
        # off the event loop.
        # in_app_notifications, NOT notifications_sent. _normalize_portal_alert
        # reads created_at/title/body/severity/device_serial, which are exactly
        # this table's columns — notifications_sent has none of them (it is
        # id, source_type, source_id, threshold, sent_at, recipient, details),
        # so every portal card rendered as an empty "Portal notification".
        #
        # This is also the alert engine's real portal-side output, it carries
        # the friendly device name, and it has no per-recipient fan-out — the
        # email ledger writes one row per recipient, so N recipients meant N
        # near-identical cards for one event. The email ledger is still shown,
        # with its own columns and a Recipient column, at /notifications/.
        raw_history = await run_in_threadpool(db.get_in_app_notifications, limit=50)
        portal_history = [
            _normalize_portal_alert(h) for h in raw_history if isinstance(h, dict)
        ]
        if severity_filter:
            portal_history = [
                p for p in portal_history if p.get("severity") == severity_filter
            ]
        if q:
            portal_history = filter_items(portal_history, q, "title", "body", "device", "device_serial")
    except Exception as exc:
        logger.warning("Portal notification history unavailable: %s", exc)

    timeline = sorted(
        filtered + portal_history,
        key=lambda a: _parse_time(a.get("time")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:30]

    ctx = {
        "central_alerts": central_alerts,
        "portal_history": portal_history,
        "timeline": timeline,
        "summary": summary,
        "severity_filter": severity_filter or "",
        "q": q,
    }
    if pg:
        ctx.update({
            "page": pg["page"],
            "per_page": pg["per_page"],
            "total": pg["total"],
            "total_pages": pg["total_pages"],
            "has_prev": pg["has_prev"],
            "has_next": pg["has_next"],
            "base_qs": pg["base_qs"],
        })
    return ctx


def _render_alerts_fragment(request: Request, context: dict) -> HTMLResponse:
    template = templates.env.get_template("alerts/hub.html")
    block = template.blocks["alerts_live"]
    ctx = template.new_context({"request": request, **context})
    return HTMLResponse("".join(block(ctx)))


@router.get("/")
async def alerts_hub(request: Request, partial: int = 0, severity: str = "", q: str = ""):
    sev = severity.strip().lower() if severity else None
    if sev and sev not in ("critical", "major", "minor", "other"):
        sev = None
    query = q.strip()

    ctx = await _load_alerts_context(severity_filter=sev, q=query, request=request)
    ctx["active"] = "alerts"
    ctx["is_partial"] = bool(partial)

    if partial:
        return _render_alerts_fragment(request, ctx)

    return templates.TemplateResponse(request, "alerts/hub.html", ctx)
