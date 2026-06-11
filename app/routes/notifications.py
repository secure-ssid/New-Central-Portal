"""Routes for expiry notification settings and dashboard."""
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
import db

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Simple server-side email validation (pragmatic, not RFC-exhaustive).
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_SETTING_KEYS = (
    "thresholds", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
    "smtp_from", "smtp_tls", "check_subscriptions", "check_ssl", "ssl_hosts",
)

_SETTING_DEFAULTS = {
    "thresholds": "90,60,30,15",
    "smtp_port": "587",
    "smtp_tls": "true",
    "check_subscriptions": "true",
    "check_ssl": "true",
}

# Device-type filter values accepted for alert rules.
_VALID_DEVICE_TYPES = {"", "all", "switch", "ap", "gateway"}

_DEFAULT_REPORT_CFG = {"id": 1, "enabled": False, "frequency": "daily", "hour": 8, "last_sent": None}


def _jsonable_report_cfg(cfg: dict) -> dict:
    out = dict(cfg)
    if hasattr(out.get("last_sent"), "isoformat"):
        out["last_sent"] = out["last_sent"].isoformat()
    return out


def _age_str(created, now) -> str:
    """Human age like '4m ago' for the notification bell."""
    if not hasattr(created, "tzinfo"):
        return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - created).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _validate_rule_fields(body: dict, partial: bool = False) -> tuple[dict | None, str | None]:
    """Validate alert-rule payload. Returns (fields, error)."""
    out: dict = {}
    if "site_filter" in body or not partial:
        sf = str(body.get("site_filter") or "").strip()
        if len(sf) > 200:
            return None, "site_filter must be 200 characters or fewer"
        out["site_filter"] = sf if sf and sf.lower() != "all" else None
    if "device_type_filter" in body or not partial:
        tf = str(body.get("device_type_filter") or "").strip().lower()
        if tf not in _VALID_DEVICE_TYPES:
            return None, "device_type_filter must be one of all/switch/ap/gateway"
        out["device_type_filter"] = tf if tf and tf != "all" else None
    for field, default in (("offline_minutes", 5), ("cooldown_minutes", 60)):
        if field in body or not partial:
            try:
                v = int(body.get(field, default))
            except (TypeError, ValueError):
                return None, f"{field} must be an integer"
            if not 1 <= v <= 1440:
                return None, f"{field} must be between 1 and 1440"
            out[field] = v
    if "enabled" in body:
        out["enabled"] = body.get("enabled") in (True, "true", "True", 1)
    return out, None


async def _read_json(request: Request) -> dict | None:
    """Parse a JSON body; return None on malformed/non-object payloads."""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@router.get("/")
async def notifications_page(request: Request):
    """Notification settings + expiry dashboard."""
    db_error = False
    settings = {}
    recipients: list[dict] = []
    history: list[dict] = []
    rules: list[dict] = []
    report_cfg = dict(_DEFAULT_REPORT_CFG)
    try:
        settings = {k: db.get_setting(k) for k in _SETTING_KEYS}
        recipients = db.get_recipients()
        history = db.get_notification_history(limit=50)
    except Exception as exc:
        logger.error("Notifications page: database unavailable: %s", exc)
        db_error = True
        settings = {k: _SETTING_DEFAULTS.get(k, "") for k in _SETTING_KEYS}
    if not db_error:
        try:
            rules = db.get_alert_rules()
            report_cfg = _jsonable_report_cfg(db.get_report_settings())
        except Exception as exc:
            logger.error("Notifications page: could not load alert rules/report settings: %s", exc)

    # Site names for the rule filter dropdown (best effort — UI falls back to
    # free-text entry when empty).
    site_names: list[str] = []
    try:
        from vendors.central_bridge import get_central_sites
        raw_sites = await get_central_sites()
        site_names = sorted(
            {str(s.get("site_name") or s.get("name") or "").strip()
             for s in raw_sites if isinstance(s, dict)} - {""},
            key=str.lower,
        )
    except Exception as exc:
        logger.warning("Notifications page: could not fetch sites: %s", exc)

    # Fetch upcoming expirations for the dashboard
    upcoming_subs = []
    try:
        from vendors.central_bridge import get_glp_subscriptions
        subs = await get_glp_subscriptions()
        now = datetime.now(timezone.utc)
        for s in subs:
            if not isinstance(s, dict):
                continue
            end_str = s.get("endTime") or ""
            if not end_str:
                continue
            try:
                end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            days_left = (end_date - now).days
            if days_left <= 90:
                upcoming_subs.append({
                    "key": s.get("key", ""),
                    "tier": s.get("tier") or s.get("subscriptionType") or "",
                    "end_date": end_str[:10],
                    "days_left": days_left,
                    "quantity": s.get("quantity", 0),
                    "available": s.get("availableQuantity", 0),
                })
        upcoming_subs.sort(key=lambda x: x["days_left"])
    except Exception as exc:
        logger.warning("Notifications page: could not fetch GLP subscriptions: %s", exc)

    # Check SSL certs for the dashboard
    upcoming_certs = []
    ssl_hosts_str = settings.get("ssl_hosts", "")
    if ssl_hosts_str.strip():
        import ssl as _ssl
        import socket
        for host in [h.strip() for h in ssl_hosts_str.split(",") if h.strip()]:
            hostname = host.split(":")[0]
            try:
                port = int(host.split(":")[1]) if ":" in host else 443
            except ValueError:
                port = 443
            try:
                ctx = _ssl.create_default_context()
                with socket.create_connection((hostname, port), timeout=5) as sock:
                    with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                        cert = ssock.getpeercert()
                not_after_str = cert.get("notAfter", "")
                not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                days_left = (not_after - now).days
                if days_left <= 90:
                    upcoming_certs.append({
                        "hostname": f"{hostname}:{port}",
                        "end_date": not_after.strftime("%Y-%m-%d"),
                        "days_left": days_left,
                    })
            except Exception as exc:
                logger.warning("SSL dashboard check failed for %s:%s: %s", hostname, port, exc)
                upcoming_certs.append({
                    "hostname": f"{hostname}:{port}",
                    "end_date": "ERROR",
                    "days_left": -1,
                })
        upcoming_certs.sort(key=lambda x: x["days_left"])

    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "active": "notifications",
            "settings": settings,
            "recipients": recipients,
            "history": history,
            "upcoming_subs": upcoming_subs,
            "upcoming_certs": upcoming_certs,
            "rules": rules,
            "report_cfg": report_cfg,
            "site_names": site_names,
            "db_error": db_error,
            "warning": "Database unavailable — settings shown are defaults and changes cannot be saved." if db_error else "",
        },
    )


@router.post("/settings")
async def save_settings(request: Request):
    """Save notification settings."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    allowed = set(_SETTING_KEYS)
    try:
        for k, v in body.items():
            if k in allowed:
                db.set_setting(k, str(v))
    except Exception as exc:
        logger.error("Failed to save notification settings: %s", exc)
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=503)
    return JSONResponse({"ok": True})


@router.post("/recipients")
async def manage_recipients(request: Request):
    """Add or remove a recipient."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    action = body.get("action", "")
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"ok": False, "error": "Invalid email"}, status_code=400)
    try:
        if action == "add":
            db.add_recipient(email)
        elif action == "remove":
            db.remove_recipient(email)
        else:
            return JSONResponse({"ok": False, "error": "action must be 'add' or 'remove'"}, status_code=400)
    except Exception as exc:
        logger.error("Failed to %s recipient %s: %s", action, email, exc)
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=503)
    return JSONResponse({"ok": True})


@router.post("/test-email")
async def test_email(request: Request):
    """Send a test email to verify SMTP settings."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    to = str(body.get("email", "")).strip()
    if not EMAIL_RE.match(to):
        return JSONResponse({"ok": False, "error": "Invalid email"}, status_code=400)
    from notifications import _send_email
    try:
        ok = _send_email(to, "🔔 Test — New Central Portal Notifications", """
        <div style="font-family:system-ui,sans-serif;padding:24px;max-width:500px;margin:0 auto;">
            <h2 style="color:#f97316;margin:0 0 12px;">Test Email ✓</h2>
            <p style="color:#555;">If you received this, your SMTP settings are configured correctly.</p>
            <p style="color:#999;font-size:12px;margin-top:20px;">Sent from New Central Portal</p>
        </div>
        """)
    except Exception as exc:
        logger.error("Test email to %s failed: %s", to, exc)
        ok = False
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "SMTP send failed — check server logs. Gmail requires an App Password if 2FA is on."})


@router.post("/check-now")
async def check_now(request: Request):
    """Manually trigger an expiry check."""
    from notifications import run_expiry_check
    # Pre-fetch subs async so we don't hit "event loop already running"
    subs = []
    try:
        from vendors.central_bridge import get_glp_subscriptions
        subs = await get_glp_subscriptions()
    except Exception as exc:
        logger.warning("check-now: could not fetch GLP subscriptions: %s", exc)
    try:
        alerts = run_expiry_check(subs=subs)
    except Exception as exc:
        logger.error("Manual expiry check failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Expiry check failed — see server logs"}, status_code=500)
    return JSONResponse({"ok": True, "alerts": alerts})


# ── Alert rules (device-down) ────────────────────────────────────────────────

@router.post("/rules")
async def create_rule(request: Request):
    """Create a device-down alert rule."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    fields, err = _validate_rule_fields(body, partial=False)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        rule = db.add_alert_rule(
            fields.get("site_filter"), fields.get("device_type_filter"),
            fields["offline_minutes"], fields["cooldown_minutes"],
        )
    except Exception as exc:
        logger.error("Failed to create alert rule: %s", exc)
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=503)
    return JSONResponse({"ok": True, "rule": rule})


@router.patch("/rules/{rule_id}")
async def update_rule(request: Request, rule_id: int):
    """Update fields of an alert rule (e.g. enable/disable)."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    fields, err = _validate_rule_fields(body, partial=True)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    if not fields:
        return JSONResponse({"ok": False, "error": "No valid fields to update"}, status_code=400)
    try:
        found = db.update_alert_rule(rule_id, fields)
    except Exception as exc:
        logger.error("Failed to update alert rule %s: %s", rule_id, exc)
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=503)
    if not found:
        return JSONResponse({"ok": False, "error": "Rule not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    """Delete an alert rule."""
    try:
        found = db.delete_alert_rule(rule_id)
    except Exception as exc:
        logger.error("Failed to delete alert rule %s: %s", rule_id, exc)
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=503)
    if not found:
        return JSONResponse({"ok": False, "error": "Rule not found"}, status_code=404)
    return JSONResponse({"ok": True})


# ── In-app notification API (contract for the notification bell UI) ─────────

@router.get("/api/recent")
async def api_recent():
    """Recent in-app notifications, newest first, capped at 15.

    Contract: {"items": [{id, title, body, severity, device_serial, url,
    created_at_iso, age, read}], "unread": N}. Degrades to empty/0 if the
    database is unavailable.
    """
    try:
        rows = db.get_in_app_notifications(limit=15)
        unread = db.count_unread_notifications()
    except Exception as exc:
        logger.warning("api/recent: database unavailable: %s", exc)
        return JSONResponse({"items": [], "unread": 0})
    now = datetime.now(timezone.utc)
    items = []
    for n in rows:
        created = n.get("created_at")
        items.append({
            "id": n.get("id"),
            "title": n.get("title") or "",
            "body": n.get("body") or "",
            "severity": n.get("severity") or "info",
            "device_serial": n.get("device_serial"),
            "url": n.get("url"),
            "created_at_iso": created.isoformat() if hasattr(created, "isoformat") else "",
            "age": _age_str(created, now),
            "read": bool(n.get("read")),
        })
    return JSONResponse({"items": items, "unread": unread})


@router.post("/api/mark-read")
async def api_mark_read(request: Request):
    """Mark notifications read: {"ids": [..]} or {"all": true}."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    mark_all = body.get("all") is True
    ids: list[int] = []
    if not mark_all:
        raw_ids = body.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return JSONResponse({"ok": False, "error": "Provide ids (non-empty list) or all:true"},
                                status_code=400)
        try:
            ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "ids must be integers"}, status_code=400)
    try:
        unread = db.mark_notifications_read(ids=ids or None, mark_all=mark_all)
    except Exception as exc:
        logger.error("api/mark-read failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Database unavailable", "unread": 0},
                            status_code=503)
    return JSONResponse({"ok": True, "unread": unread})


# ── Summary reports ──────────────────────────────────────────────────────────

@router.post("/reports/settings")
async def save_report_settings(request: Request):
    """Save summary-report settings (enabled / frequency / hour)."""
    body = await _read_json(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    enabled = None
    if "enabled" in body:
        enabled = body.get("enabled") in (True, "true", "True", 1)
    frequency = None
    if "frequency" in body:
        frequency = str(body.get("frequency") or "").strip().lower()
        if frequency not in ("daily", "weekly"):
            return JSONResponse({"ok": False, "error": "frequency must be daily or weekly"},
                                status_code=400)
    hour = None
    if "hour" in body:
        try:
            hour = int(body.get("hour"))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "hour must be an integer"}, status_code=400)
        if not 0 <= hour <= 23:
            return JSONResponse({"ok": False, "error": "hour must be between 0 and 23"}, status_code=400)
    try:
        db.save_report_settings(enabled=enabled, frequency=frequency, hour=hour)
        cfg = _jsonable_report_cfg(db.get_report_settings())
    except Exception as exc:
        logger.error("Failed to save report settings: %s", exc)
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=503)
    return JSONResponse({"ok": True, "settings": cfg})


@router.post("/reports/test")
async def send_test_report():
    """Send a summary report immediately to all recipients."""
    # Pre-fetch async so run_summary_report doesn't open a nested event loop.
    devices: list[dict] = []
    subs: list[dict] = []
    try:
        from vendors.central_bridge import get_devices
        devices = await get_devices(limit=1000)
    except Exception as exc:
        logger.warning("reports/test: could not fetch devices: %s", exc)
    try:
        from vendors.central_bridge import get_glp_subscriptions
        subs = await get_glp_subscriptions()
    except Exception as exc:
        logger.warning("reports/test: could not fetch GLP subscriptions: %s", exc)
    from notifications import run_summary_report
    try:
        result = run_summary_report(force=True, devices=devices, subs=subs)
    except Exception as exc:
        logger.error("Test summary report failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Report failed — see server logs"}, status_code=500)
    return JSONResponse(result)
