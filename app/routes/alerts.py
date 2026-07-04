"""Unified Alerts hub — Central active alerts + portal notification history."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

import db

from templates_shared import templates

router = APIRouter()
logger = logging.getLogger(__name__)


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
    return {
        "source": "central",
        "id": raw.get("id") or raw.get("alertId") or raw.get("alert_id") or "",
        "title": raw.get("title") or raw.get("alertName") or raw.get("name") or "Alert",
        "body": raw.get("description") or raw.get("message") or "",
        "severity": _severity_class(str(raw.get("severity") or raw.get("alertSeverity") or "")),
        "device": raw.get("deviceName") or raw.get("device_name") or raw.get("serialNumber") or "",
        "site": raw.get("siteName") or raw.get("site_name") or "",
        "time": raw.get("timeAt") or raw.get("createdAt") or raw.get("timestamp") or "",
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
        "site": "",
        "time": time_str,
    }


@router.get("/")
async def alerts_hub(request: Request):
    central_alerts: list[dict] = []
    portal_history: list[dict] = []
    summary = {"total": 0, "critical": 0, "major": 0, "minor": 0, "other": 0}

    try:
        from vendors.central_bridge import list_active_alerts
        raw = await list_active_alerts(limit=100)
        central_alerts = [_normalize_central_alert(a) for a in raw if isinstance(a, dict)]
    except Exception as exc:
        logger.warning("Central alerts unavailable: %s", exc)

    try:
        portal_history = [
            _normalize_portal_alert(h)
            for h in db.get_notification_history(limit=50)
            if isinstance(h, dict)
        ]
    except Exception as exc:
        logger.warning("Portal notification history unavailable: %s", exc)

    for alert in central_alerts:
        summary["total"] += 1
        sev = alert.get("severity", "other")
        if sev in summary:
            summary[sev] += 1
        else:
            summary["other"] += 1

    return templates.TemplateResponse(
        request,
        "alerts/hub.html",
        {
            "central_alerts": central_alerts,
            "portal_history": portal_history,
            "summary": summary,
            "active": "alerts",
        },
    )
