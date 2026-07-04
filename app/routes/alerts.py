"""Unified Alerts hub — Central active alerts + portal notification history."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pagination import filter_items

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


def _severity_class(sev: str) -> str:
    s = (sev or "").lower()
    if s in ("critical", "crit"):
        return "critical"
    if s in ("major", "high", "warning", "warn"):
        return "major"
    if s in ("minor", "medium", "low"):
        return "minor"
    return "other"


def _normalize_central_alert(raw: dict) -> dict:
    time_raw = raw.get("timeAt") or raw.get("createdAt") or raw.get("timestamp") or ""
    device = raw.get("deviceName") or raw.get("device_name") or raw.get("serialNumber") or ""
    serial = raw.get("serialNumber") or raw.get("serial") or raw.get("device_serial") or ""
    return {
        "source": "central",
        "id": raw.get("id") or raw.get("alertId") or raw.get("alert_id") or "",
        "title": raw.get("title") or raw.get("alertName") or raw.get("name") or "Alert",
        "body": raw.get("description") or raw.get("message") or "",
        "severity": _severity_class(str(raw.get("severity") or raw.get("alertSeverity") or "")),
        "device": device,
        "device_serial": serial,
        "site": raw.get("siteName") or raw.get("site_name") or "",
        "time": time_raw,
        "time_ago": _time_ago(time_raw),
    }


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


async def _load_alerts_context(severity_filter: str | None = None, q: str = "") -> dict:
    central_alerts: list[dict] = []
    portal_history: list[dict] = []
    summary = {"total": 0, "critical": 0, "major": 0, "minor": 0, "other": 0}

    try:
        from vendors.central_bridge import list_active_alerts
        raw = await list_active_alerts(limit=100)
        all_central = [_normalize_central_alert(a) for a in raw if isinstance(a, dict)]
        for alert in all_central:
            sev = alert.get("severity", "other")
            summary["total"] += 1
            if sev in summary:
                summary[sev] += 1
            else:
                summary["other"] += 1
        filtered = all_central
        if severity_filter:
            filtered = [a for a in filtered if a.get("severity") == severity_filter]
        if q:
            filtered = filter_items(filtered, q, "title", "body", "device", "device_serial", "site")
        central_alerts = filtered
    except Exception as exc:
        logger.warning("Central alerts unavailable: %s", exc)

    try:
        portal_history = [
            _normalize_portal_alert(h)
            for h in db.get_notification_history(limit=50)
            if isinstance(h, dict)
        ]
        if q:
            portal_history = filter_items(portal_history, q, "title", "body", "device", "device_serial")
    except Exception as exc:
        logger.warning("Portal notification history unavailable: %s", exc)

    timeline = sorted(
        central_alerts + ([] if severity_filter else portal_history),
        key=lambda a: _parse_time(a.get("time")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:30]

    return {
        "central_alerts": central_alerts,
        "portal_history": portal_history if not severity_filter else [],
        "timeline": timeline,
        "summary": summary,
        "severity_filter": severity_filter or "",
        "q": q,
    }


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

    ctx = await _load_alerts_context(severity_filter=sev, q=query)
    ctx["active"] = "alerts"
    ctx["is_partial"] = bool(partial)

    if partial:
        return _render_alerts_fragment(request, ctx)

    return templates.TemplateResponse(request, "alerts/hub.html", ctx)
