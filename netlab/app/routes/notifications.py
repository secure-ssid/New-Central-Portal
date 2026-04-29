"""Routes for expiry notification settings and dashboard."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timezone
import db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def notifications_page(request: Request):
    """Notification settings + expiry dashboard."""
    settings = {
        "thresholds": db.get_setting("thresholds"),
        "smtp_host": db.get_setting("smtp_host"),
        "smtp_port": db.get_setting("smtp_port"),
        "smtp_user": db.get_setting("smtp_user"),
        "smtp_password": db.get_setting("smtp_password"),
        "smtp_from": db.get_setting("smtp_from"),
        "smtp_tls": db.get_setting("smtp_tls"),
        "check_subscriptions": db.get_setting("check_subscriptions"),
        "check_ssl": db.get_setting("check_ssl"),
        "ssl_hosts": db.get_setting("ssl_hosts"),
    }
    recipients = db.get_recipients()
    history = db.get_notification_history(limit=50)

    # Fetch upcoming expirations for the dashboard
    upcoming_subs = []
    try:
        from vendors.central_bridge import get_glp_subscriptions
        subs = await get_glp_subscriptions()
        now = datetime.now(timezone.utc)
        for s in subs:
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
    except Exception:
        pass

    # Check SSL certs for the dashboard
    upcoming_certs = []
    ssl_hosts_str = settings["ssl_hosts"]
    if ssl_hosts_str.strip():
        import ssl as _ssl
        import socket
        for host in [h.strip() for h in ssl_hosts_str.split(",") if h.strip()]:
            hostname = host.split(":")[0]
            port = int(host.split(":")[1]) if ":" in host else 443
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
            except Exception:
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
        },
    )


@router.post("/settings")
async def save_settings(request: Request):
    """Save notification settings."""
    body = await request.json()
    allowed = {
        "thresholds", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
        "smtp_from", "smtp_tls", "check_subscriptions", "check_ssl", "ssl_hosts",
    }
    for k, v in body.items():
        if k in allowed:
            db.set_setting(k, str(v))
    return JSONResponse({"ok": True})


@router.post("/recipients")
async def manage_recipients(request: Request):
    """Add or remove a recipient."""
    body = await request.json()
    action = body.get("action", "")
    email = body.get("email", "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "Invalid email"}, status_code=400)
    if action == "add":
        db.add_recipient(email)
    elif action == "remove":
        db.remove_recipient(email)
    else:
        return JSONResponse({"ok": False, "error": "action must be 'add' or 'remove'"}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/test-email")
async def test_email(request: Request):
    """Send a test email to verify SMTP settings."""
    body = await request.json()
    to = body.get("email", "").strip()
    if not to:
        return JSONResponse({"ok": False, "error": "Email required"}, status_code=400)
    from notifications import _send_email
    ok = _send_email(to, "🔔 Test — New Central Portal Notifications", """
    <div style="font-family:system-ui,sans-serif;padding:24px;max-width:500px;margin:0 auto;">
        <h2 style="color:#f97316;margin:0 0 12px;">Test Email ✓</h2>
        <p style="color:#555;">If you received this, your SMTP settings are configured correctly.</p>
        <p style="color:#999;font-size:12px;margin-top:20px;">Sent from New Central Portal</p>
    </div>
    """)
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
    except Exception:
        pass
    alerts = run_expiry_check(subs=subs)
    return JSONResponse({"ok": True, "alerts": alerts})
