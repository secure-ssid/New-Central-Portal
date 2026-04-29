"""Expiry notification checker — GLP subscriptions + SSL certificates."""
import logging
import smtplib
import ssl
import socket
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import db

logger = logging.getLogger(__name__)

# ── Email sender ─────────────────────────────────────────────────────────────

def _send_email(to: str, subject: str, html_body: str):
    """Send an email via configured SMTP."""
    host = db.get_setting("smtp_host")
    port = int(db.get_setting("smtp_port") or "587")
    user = db.get_setting("smtp_user")
    password = db.get_setting("smtp_password")
    from_addr = db.get_setting("smtp_from") or user
    use_tls = db.get_setting("smtp_tls") != "false"

    if not host or not user:
        logger.warning("SMTP not configured — skipping email to %s: %s", to, subject)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        if use_tls:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls(context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
        server.login(user, password)
        server.sendmail(from_addr, [to], msg.as_string())
        server.quit()
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to, e)
        return False


# ── Subscription expiry check ────────────────────────────────────────────────

def check_subscriptions(subs: list[dict] | None = None):
    """Check GLP subscriptions for upcoming expirations.
    
    If subs is None, fetches them (only works outside an async event loop).
    Pass pre-fetched subs when calling from an async context.
    """
    if db.get_setting("check_subscriptions") != "true":
        return []

    thresholds = [int(t) for t in db.get_setting("thresholds").split(",") if t.strip()]
    recipients = db.get_recipients()
    if not recipients or not thresholds:
        return []

    if subs is None:
        try:
            from vendors.central_bridge import get_glp_subscriptions
            import asyncio
            loop = asyncio.new_event_loop()
            subs = loop.run_until_complete(get_glp_subscriptions())
            loop.close()
        except Exception as e:
            logger.error("Failed to fetch GLP subscriptions: %s", e)
            return []

    now = datetime.now(timezone.utc)
    alerts = []

    # Group expiring subs by threshold per recipient — one email per threshold
    # Only include subs that are actually in use (assigned qty > 0)
    expiring_by_threshold: dict[int, list[dict]] = {}

    for sub in subs:
        end_str = sub.get("endTime") or ""
        if not end_str:
            continue

        qty = int(sub.get("quantity") or 0)
        avail = int(sub.get("availableQuantity") or 0)
        in_use = qty - avail
        if in_use <= 0:
            continue  # skip unused subscriptions

        try:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        days_left = (end_date - now).days
        sub_key = sub.get("key", "unknown")
        tier = sub.get("tier") or sub.get("subscriptionType") or ""

        for threshold in sorted(thresholds, reverse=True):
            if days_left <= threshold:
                expiring_by_threshold.setdefault(threshold, []).append({
                    "key": sub_key, "tier": tier, "days_left": days_left,
                    "end_date": end_str[:10], "quantity": qty,
                    "available": avail, "in_use": in_use,
                })
                break

    # Send one grouped email per threshold per recipient
    for threshold, sub_list in sorted(expiring_by_threshold.items(), reverse=True):
        for recip in recipients:
            email = recip["email"]
            # Use a batch key so we don't re-send the same threshold batch
            batch_key = f"batch_{threshold}d"
            if db.was_notified("subscription_batch", batch_key, threshold, email):
                continue

            sub_list.sort(key=lambda s: s["days_left"])
            min_days = sub_list[0]["days_left"]
            subject = f"⚠️ {len(sub_list)} GreenLake Subscriptions Expiring — {min_days} days"
            html = _sub_batch_email_html(sub_list, threshold)
            sent = _send_email(email, subject, html)

            if sent:
                db.record_notification(
                    "subscription_batch", batch_key, threshold, email,
                    f"{len(sub_list)} subs, earliest {min_days}d left"
                )
            alerts.append({
                "type": "subscription_batch", "id": batch_key,
                "count": len(sub_list), "threshold": threshold,
                "recipient": email, "sent": sent,
            })
    return alerts


def _sub_batch_email_html(subs: list[dict], threshold: int) -> str:
    min_days = subs[0]["days_left"]
    urgency = "🔴" if min_days <= 15 else "🟡" if min_days <= 30 else "🟠"

    rows = ""
    for s in subs:
        day_color = "#ef4444" if s["days_left"] <= 15 else "#f59e0b" if s["days_left"] <= 30 else "#fb923c"
        rows += f"""
            <tr style="border-bottom:1px solid #eee;">
                <td style="padding:8px;font-family:monospace;font-size:13px;">{s['key']}</td>
                <td style="padding:8px;font-size:13px;">{s['tier']}</td>
                <td style="padding:8px;font-size:13px;">{s['end_date']}</td>
                <td style="padding:8px;font-weight:bold;color:{day_color};font-size:13px;">{s['days_left']}d</td>
                <td style="padding:8px;font-size:13px;">{s['in_use']} / {s['quantity']}</td>
            </tr>"""

    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:700px;margin:0 auto;padding:24px;">
        <h2 style="color:#f59e0b;margin:0 0 4px;">{urgency} {len(subs)} Subscriptions Expiring</h2>
        <p style="color:#666;font-size:14px;margin:0 0 20px;">The following in-use GreenLake subscriptions are expiring within {threshold} days.</p>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <thead>
                <tr style="background:#f8f8f8;border-bottom:2px solid #ddd;">
                    <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Key</th>
                    <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Tier</th>
                    <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Expires</th>
                    <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Days Left</th>
                    <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">In Use / Total</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        <p style="margin:20px 0 0;padding:12px;background:#fef3c7;border-radius:8px;color:#92400e;font-size:13px;">
            This is an automated {threshold}-day expiry notice from New Central Portal.
            Only subscriptions with active assignments are shown. Please renew before they expire.
        </p>
    </div>
    """


# ── SSL certificate expiry check ────────────────────────────────────────────

def check_ssl_certs():
    """Check configured SSL endpoints for certificate expiration."""
    if db.get_setting("check_ssl") != "true":
        return []

    hosts_str = db.get_setting("ssl_hosts")
    if not hosts_str.strip():
        return []

    hosts = [h.strip() for h in hosts_str.split(",") if h.strip()]
    thresholds = [int(t) for t in db.get_setting("thresholds").split(",") if t.strip()]
    recipients = db.get_recipients()
    if not recipients or not thresholds:
        return []

    now = datetime.now(timezone.utc)
    alerts = []

    for host in hosts:
        hostname = host.split(":")[0]
        port = int(host.split(":")[1]) if ":" in host else 443

        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((hostname, port), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                    cert = ssock.getpeercert()
            not_after_str = cert.get("notAfter", "")
            # Format: 'Dec 31 23:59:59 2025 GMT'
            not_after = datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.error("SSL check failed for %s: %s", host, e)
            continue

        days_left = (not_after - now).days

        for threshold in sorted(thresholds, reverse=True):
            if days_left <= threshold:
                for recip in recipients:
                    email = recip["email"]
                    if db.was_notified("ssl_cert", hostname, threshold, email):
                        continue

                    subject = f"🔒 SSL Certificate Expiring in {days_left} Days — {hostname}"
                    html = _ssl_email_html(hostname, port, days_left, threshold, not_after)
                    sent = _send_email(email, subject, html)

                    if sent:
                        db.record_notification(
                            "ssl_cert", hostname, threshold, email,
                            f"Cert expires {not_after.strftime('%Y-%m-%d')}, {days_left}d left"
                        )
                    alerts.append({
                        "type": "ssl_cert", "id": hostname, "days_left": days_left,
                        "threshold": threshold, "recipient": email, "sent": sent,
                    })
                break
    return alerts


def _ssl_email_html(hostname: str, port: int, days_left: int, threshold: int, not_after: datetime) -> str:
    urgency = "🔴" if days_left <= 15 else "🟡" if days_left <= 30 else "🟠"
    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
        <h2 style="color:#ef4444;margin:0 0 16px;">{urgency} SSL Certificate Expiring</h2>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <tr><td style="padding:8px;color:#666;width:140px;">Hostname</td>
                <td style="padding:8px;font-weight:bold;font-family:monospace;">{hostname}:{port}</td></tr>
            <tr><td style="padding:8px;color:#666;">Expires</td>
                <td style="padding:8px;font-weight:bold;color:#ef4444;">{not_after.strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
            <tr><td style="padding:8px;color:#666;">Days Remaining</td>
                <td style="padding:8px;font-weight:bold;">{days_left}</td></tr>
        </table>
        <p style="margin:20px 0 0;padding:12px;background:#fee2e2;border-radius:8px;color:#991b1b;font-size:13px;">
            This is an automated {threshold}-day expiry notice from New Central Portal.
            Please renew this certificate before it expires.
        </p>
    </div>
    """


# ── Main checker (called by scheduler) ──────────────────────────────────────

def run_expiry_check(subs: list[dict] | None = None):
    """Run all expiry checks.
    
    Called by APScheduler (subs=None, fetches its own) or by the
    async route (passes pre-fetched subs to avoid event loop conflict).
    """
    logger.info("Running expiry notification check…")
    sub_alerts = check_subscriptions(subs=subs)
    ssl_alerts = check_ssl_certs()
    total = len(sub_alerts) + len(ssl_alerts)
    sent = sum(1 for a in sub_alerts + ssl_alerts if a.get("sent"))
    logger.info("Expiry check complete: %d alerts, %d emails sent", total, sent)
    return sub_alerts + ssl_alerts
