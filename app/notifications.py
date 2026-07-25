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


def _bucket_for(days_left: int, thresholds: list[int]) -> int | None:
    """The warning gate an item is actually sitting in, or None if not due yet.

    Thresholds are "days until expiry" gates (default 90,60,30,15) and the test
    is ``days_left <= threshold``, so an item matches EVERY gate at or above its
    remaining life. The band it is really in is therefore the TIGHTEST match.

    Both callers used to scan ``sorted(thresholds, reverse=True)`` and break on
    the first hit, which takes the LOOSEST — so an item that first appeared at
    85 days was bucketed at 90 and stayed bucketed at 90 through 59, 29, 14 and
    past expiry. Combined with a dedup row keyed on that threshold, each item
    got exactly one email in its whole life, at the coarsest gate it ever
    crossed, and was then silent right through expiring. Escalation was
    impossible, which is what the "90, 60, 30, 15 day warnings" copy on the
    settings page and the ``threshold`` column in notifications_sent both
    assume works.
    """
    matching = [t for t in thresholds if days_left <= t]
    return min(matching) if matching else None


def _expiry_dedup_id(identity: str, expires_at: datetime) -> str:
    """Dedup key identifying the EVENT: what expires, and when.

    ``notifications_sent`` is UNIQUE(source_type, source_id, threshold,
    recipient) and nothing ever deletes from it — there is no TTL, no retention
    job and no admin reset. So whatever goes in source_id suppresses that alert
    permanently.

    The old keys carried no event identity at all: subscriptions used the
    constant ``f"batch_{threshold}d"``, meaning one 90-day batch email per
    recipient for the lifetime of the database, and SSL used the bare hostname,
    so a host went silent forever after its first certificate. Keying on the
    expiry instant means a renewal (new end date, new notAfter) starts a fresh
    ladder, while re-runs within the same expiry event stay suppressed.

    ``_fire_down_alert`` already used this shape (``serial@timestamp``); this is
    the same idea applied to the expiry paths.
    """
    return f"{identity}@{expires_at.astimezone(timezone.utc):%Y%m%d}"


def _parse_thresholds() -> list[int]:
    """Parse the thresholds setting defensively (ignore junk values)."""
    out = []
    for t in (db.get_setting("thresholds") or "").split(","):
        t = t.strip()
        if not t:
            continue
        try:
            out.append(int(t))
        except ValueError:
            logger.warning("Ignoring invalid threshold value: %r", t)
    return out


# ── Email sender ─────────────────────────────────────────────────────────────

def _smtp_config() -> dict | None:
    """Read the SMTP settings, or None when the database is unreachable.

    Every alert path calls _send_email, and these six reads are the only DB
    access inside it. Left unguarded, a Postgres blip raised straight through
    _fire_down_alert into the scheduler job: alerted_at was never recorded and
    the snapshot never persisted, so once the DB returned the whole fleet was
    re-mailed every 60 seconds.
    """
    try:
        return {
            "host": db.get_setting("smtp_host"),
            "port_raw": db.get_setting("smtp_port"),
            "user": db.get_setting("smtp_user"),
            "password": db.get_setting("smtp_password"),
            "from": db.get_setting("smtp_from"),
            "tls": db.get_setting("smtp_tls"),
        }
    except Exception as e:
        logger.warning("SMTP settings unavailable (database down?): %s", e)
        return None


def _send_email(to: str, subject: str, html_body: str):
    """Send an email via configured SMTP. Never raises — returns False instead."""
    cfg = _smtp_config()
    if cfg is None:
        return False
    host = cfg["host"]
    try:
        port = int(cfg["port_raw"] or "587")
    except ValueError:
        logger.warning("Invalid smtp_port setting — falling back to 587")
        port = 587
    user = cfg["user"]
    password = cfg["password"]
    from_addr = cfg["from"] or user
    use_tls = cfg["tls"] != "false"

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

def check_subscriptions(subs: list[dict] | None = None, now: datetime | None = None):
    """Check GLP subscriptions for upcoming expirations.
    
    If subs is None, fetches them (only works outside an async event loop).
    Pass pre-fetched subs when calling from an async context.
    """
    if db.get_setting("check_subscriptions") != "true":
        return []

    thresholds = _parse_thresholds()
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

    # Injectable so the escalation ladder can be tested by advancing the
    # clock rather than by waiting 75 days. Matches run_device_status_check.
    now = now or datetime.now(timezone.utc)
    alerts = []

    # Group expiring subs by threshold per recipient — one email per threshold
    # Only include subs that are actually in use (assigned qty > 0)
    expiring_by_threshold: dict[int, list[dict]] = {}

    for sub in subs:
        if not isinstance(sub, dict):
            continue
        end_str = sub.get("endTime") or ""
        if not end_str:
            continue

        try:
            qty = int(sub.get("quantity") or 0)
            avail = int(sub.get("availableQuantity") or 0)
        except (TypeError, ValueError):
            logger.warning("Skipping subscription with non-numeric quantities: %s", sub.get("key"))
            continue
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

        threshold = _bucket_for(days_left, thresholds)
        if threshold is None:
            continue
        # GLP gives every subscription a stable id and key; prefer id, and pair
        # it with the expiry date so an extended subscription alerts again.
        identity = str(sub.get("id") or sub_key or "unknown").strip() or "unknown"
        expiring_by_threshold.setdefault(threshold, []).append({
            "key": sub_key, "tier": tier, "days_left": days_left,
            "end_date": end_str[:10], "quantity": qty,
            "available": avail, "in_use": in_use,
            "dedup_id": _expiry_dedup_id(identity, end_date),
        })

    # One grouped email per threshold per recipient, but deduped per
    # SUBSCRIPTION. The old batch key was the constant f"batch_{threshold}d",
    # so once any subscription had triggered a given gate for a recipient, no
    # future subscription could ever trigger that gate again for them.
    for threshold, sub_list in sorted(expiring_by_threshold.items(), reverse=True):
        sub_list.sort(key=lambda s: s["days_left"])
        for recip in recipients:
            email = recip["email"]
            pending = [
                s for s in sub_list
                if not db.was_notified("subscription", s["dedup_id"], threshold, email)
            ]
            if not pending:
                continue

            min_days = pending[0]["days_left"]
            subject = f"⚠️ {len(pending)} GreenLake Subscriptions Expiring — {min_days} days"
            html = _sub_batch_email_html(pending, threshold)
            sent = _send_email(email, subject, html)

            if sent:
                for s in pending:
                    db.record_notification(
                        "subscription", s["dedup_id"], threshold, email,
                        f"{s['key']} expires {s['end_date']}, {s['days_left']}d left"
                    )
            alerts.append({
                "type": "subscription", "id": f"batch_{threshold}d",
                "count": len(pending), "threshold": threshold,
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

def _probe_cert(hostname: str, port: int, timeout: int = 10) -> datetime:
    """Return a live certificate's notAfter as an aware UTC datetime.

    Split out from check_ssl_certs purely so the expiry ladder can be tested
    without a TLS server: this is the one line in that function that touches
    the network, and leaving it inline meant the escalation and dedup logic
    around it had no test at all.
    """
    ctx = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
            cert = ssock.getpeercert()
    # Format: 'Dec 31 23:59:59 2025 GMT'
    return datetime.strptime(
        cert.get("notAfter", ""), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=timezone.utc)


def check_ssl_certs(now: datetime | None = None):
    """Check configured SSL endpoints for certificate expiration."""
    if db.get_setting("check_ssl") != "true":
        return []

    hosts_str = db.get_setting("ssl_hosts")
    if not hosts_str.strip():
        return []

    hosts = [h.strip() for h in hosts_str.split(",") if h.strip()]
    thresholds = _parse_thresholds()
    recipients = db.get_recipients()
    if not recipients or not thresholds:
        return []

    # Injectable so the escalation ladder can be tested by advancing the
    # clock rather than by waiting 75 days. Matches run_device_status_check.
    now = now or datetime.now(timezone.utc)
    alerts = []

    for host in hosts:
        hostname = host.split(":")[0]
        try:
            port = int(host.split(":")[1]) if ":" in host else 443
        except ValueError:
            logger.warning("Invalid port in ssl_hosts entry %r — using 443", host)
            port = 443

        try:
            not_after = _probe_cert(hostname, port)
        except Exception as e:
            logger.error("SSL check failed for %s: %s", host, e)
            continue

        days_left = (not_after - now).days

        threshold = _bucket_for(days_left, thresholds)
        if threshold is None:
            continue

        # Scoped to host:port AND the certificate's own expiry. Keyed on the
        # bare hostname, a host went permanently silent after its first
        # certificate: on renewal days_left jumps back to a year, and when it
        # next reaches 90 the year-old row is still there. The port matters too
        # — 443 and 8443 on one host previously shared a single dedup namespace.
        cert_id = _expiry_dedup_id(f"{hostname.lower()}:{port}", not_after)

        for recip in recipients:
            email = recip["email"]
            if db.was_notified("ssl_cert", cert_id, threshold, email):
                continue

            subject = f"🔒 SSL Certificate Expiring in {days_left} Days — {hostname}"
            html = _ssl_email_html(hostname, port, days_left, threshold, not_after)
            sent = _send_email(email, subject, html)

            if sent:
                db.record_notification(
                    "ssl_cert", cert_id, threshold, email,
                    f"Cert expires {not_after.strftime('%Y-%m-%d')}, {days_left}d left"
                )
            alerts.append({
                "type": "ssl_cert", "id": cert_id, "days_left": days_left,
                "threshold": threshold, "recipient": email, "sent": sent,
            })
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
    try:
        sub_alerts = check_subscriptions(subs=subs)
    except Exception:
        logger.exception("Subscription expiry check failed")
        sub_alerts = []
    try:
        ssl_alerts = check_ssl_certs()
    except Exception:
        logger.exception("SSL expiry check failed")
        ssl_alerts = []
    total = len(sub_alerts) + len(ssl_alerts)
    sent = sum(1 for a in sub_alerts + ssl_alerts if a.get("sent"))
    logger.info("Expiry check complete: %d alerts, %d emails sent", total, sent)
    return sub_alerts + ssl_alerts


# ═════════════════════════════════════════════════════════════════════════════
# Device-down alert engine
# ═════════════════════════════════════════════════════════════════════════════

# In-memory last-known state: serial -> {name, site, type, online, offline_since,
# alerted_at, _dirty}. Mirrors the device_status_snapshot table so a restart
# doesn't re-alert the whole fleet (baseline is seeded on first run, snapshot
# restores pending-down/alerted state). Works standalone if the DB is down.
_device_state: dict[str, dict] = {}
_state_seeded = False
_rules_cache: list[dict] = []
_rules_loaded = False  # True once rules have been read from the DB successfully
_recipients_cache: list[dict] = []

# Used only when the DB has *never* been reachable, so alerting still works in
# degraded mode. Once a successful load happens (even an empty rule list, i.e.
# alerts intentionally disabled), the cache wins.
_FALLBACK_RULE = {"id": 0, "enabled": True, "site_filter": None, "device_type_filter": None,
                  "offline_minutes": 5, "cooldown_minutes": 60}


def _reset_engine_state_for_tests():
    """Test helper — clear in-memory engine state."""
    global _state_seeded, _rules_cache, _rules_loaded, _recipients_cache
    _device_state.clear()
    _state_seeded = False
    _rules_cache = []
    _rules_loaded = False
    _recipients_cache = []


def _device_fetch_limit() -> int:
    try:
        from config import settings
        return max(1, int(settings.device_fetch_limit))
    except Exception:
        return 1000


def _norm_device_type(raw) -> str:
    t = str(raw or "").strip().lower()
    if t in ("ap", "access_point", "accesspoint", "iap") or "access" in t:
        return "ap"
    if "switch" in t:
        return "switch"
    if "gateway" in t or t in ("gw", "controller"):
        return "gateway"
    return t or "unknown"


def _is_online(status) -> bool:
    return str(status or "").strip().lower() in ("online", "up", "connected")


def _normalize_devices(devices: list[dict]) -> list[dict]:
    """Map RAW centralmcp device dicts into this module's key space.

    central_bridge.get_devices() returns serialNumber/deviceName/siteName/
    deviceType (status uppercase), while everything below reads serial/name/
    site/type. Without this the serial is always empty, every device is
    skipped, and the engine tracks nothing at all.

    Applied at the engine's public entry points rather than inside
    _fetch_devices_sync(), because devices also arrive pre-fetched from
    routes/notifications.py. _norm_device is idempotent, so an
    already-normalized list passes through unchanged.
    """
    from vendors.aruba_central import _norm_device  # the single normalizer

    out: list[dict] = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        try:
            out.append(_norm_device(d))
        except Exception:
            # _norm_device lowercases status/type; a non-string there would
            # raise on a scheduler thread and kill the whole sweep.
            logger.warning("Skipping unparseable device payload", exc_info=True)
    return out


def _rule_matches(rule: dict, dev: dict) -> bool:
    site_f = str(rule.get("site_filter") or "").strip().lower()
    if site_f and site_f != "all":
        if str(dev.get("site") or "").strip().lower() != site_f:
            return False
    type_f = str(rule.get("device_type_filter") or "").strip().lower()
    if type_f and type_f != "all":
        if _norm_device_type(dev.get("type")) != _norm_device_type(type_f):
            return False
    return True


def _matching_rule(rules: list[dict], dev: dict) -> dict | None:
    """Most aggressive (lowest offline_minutes) enabled rule matching the device."""
    matches = [r for r in rules if r.get("enabled", True) and _rule_matches(r, dev)]
    if not matches:
        return None
    return min(matches, key=lambda r: (int(r.get("offline_minutes") or 5), int(r.get("id") or 0)))


def _fetch_devices_sync() -> list[dict] | None:
    """Fetch the fleet from Central in a fresh event loop (scheduler thread).

    Returns None on failure so the caller can abort without touching state.
    """
    try:
        from vendors.central_bridge import fresh_data, get_devices
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # Alerting must see live state. The portal caches Central reads for
            # 60s to stay under the rate limit, but a device-down sweep that
            # inherited a browser tab's cached fleet would delay detection by up
            # to a full extra interval.
            with fresh_data():
                devices = loop.run_until_complete(get_devices(limit=_device_fetch_limit()))
        finally:
            loop.close()
        if not isinstance(devices, list):
            return None
        return [d for d in devices if isinstance(d, dict)]
    except Exception as e:
        logger.error("Device status check: failed to fetch devices: %s", e)
        return None


def _load_rules() -> list[dict]:
    global _rules_cache, _rules_loaded
    try:
        _rules_cache = db.get_alert_rules(enabled_only=True)
        _rules_loaded = True
    except Exception as e:
        if not _rules_loaded:
            logger.warning("Device status check: rules never loaded from DB (%s) — "
                           "using built-in default rule (5m offline / 60m cooldown)", e)
            return [dict(_FALLBACK_RULE)]
        logger.warning("Device status check: could not load alert rules (using %d cached): %s",
                       len(_rules_cache), e)
    return _rules_cache


def _load_recipients() -> list[dict]:
    global _recipients_cache
    try:
        _recipients_cache = db.get_recipients()
    except Exception as e:
        logger.warning("Device status check: could not load recipients (using %d cached): %s",
                       len(_recipients_cache), e)
    return _recipients_cache


def _record_transition_safe(serial: str, name: str, status: str):
    try:
        db.record_status_transition(serial, name, status)
    except Exception as e:
        logger.warning("Could not record status transition for %s: %s", serial, e)


def _add_in_app_safe(title: str, body: str, severity: str,
                     device_serial: str | None = None, url: str | None = None):
    try:
        db.add_in_app_notification(title, body, severity, device_serial, url)
        return True
    except Exception as e:
        logger.warning("Could not insert in-app notification %r: %s", title, e)
        return False


def _persist_snapshot():
    """Write dirty in-memory states to device_status_snapshot (best effort)."""
    dirty = [(s, st) for s, st in _device_state.items() if st.get("_dirty")]
    if not dirty:
        return
    try:
        for serial, st in dirty:
            db.upsert_device_snapshot(
                serial, st.get("name"), "online" if st.get("online") else "offline",
                st.get("offline_since"), st.get("alerted_at"),
            )
            st["_dirty"] = False
    except Exception as e:
        logger.warning("Could not persist device status snapshot: %s", e)


def _new_state(dev: dict, online: bool, offline_since, alerted_at=None) -> dict:
    return {
        "name": dev.get("name") or dev.get("serial") or "",
        "site": dev.get("site") or "",
        "type": _norm_device_type(dev.get("type")),
        "online": online,
        "offline_since": offline_since,
        "alerted_at": alerted_at,
        "_dirty": True,
    }


def _seed_baseline(devices: list[dict], now: datetime):
    """First run after boot: build baseline state without firing any alerts.

    The persisted snapshot restores offline_since/alerted_at, so an outage that
    was already alerted before a restart isn't re-alerted, and a pending-down
    timer survives the restart. Devices offline with no snapshot are treated as
    baseline-offline (no alert until they come back and go down again).
    """
    snapshot: dict[str, dict] = {}
    try:
        snapshot = db.load_device_snapshot()
    except Exception as e:
        logger.warning("Could not load device status snapshot (fresh baseline): %s", e)

    for dev in devices:
        serial = str(dev.get("serial") or "").strip()
        if not serial:
            continue
        online = _is_online(dev.get("status"))
        snap = snapshot.get(serial) or {}
        offline_since = None
        if not online:
            snap_status = str(snap.get("status") or "")
            if snap_status and not _is_online(snap_status):
                # Already offline before restart — keep the original timer.
                offline_since = snap.get("offline_since")
            elif snap_status:
                # Went offline while we were down.
                offline_since = now
            # No snapshot row: baseline-offline, never alert for this outage.
        _device_state[serial] = _new_state(dev, online, offline_since,
                                           alerted_at=snap.get("alerted_at"))


def _fire_down_alert(st: dict, serial: str, rule: dict, offline_since: datetime, now: datetime):
    name = st.get("name") or serial
    site = st.get("site") or ""
    mins = max(1, int((now - offline_since).total_seconds() // 60))
    title = f"Device offline: {name}"
    body = f"{name} ({serial}) has been offline for {mins} min" + (f" — site {site}" if site else "")
    _add_in_app_safe(title, body, "critical", device_serial=serial, url=f"/devices/{serial}")

    recipients = _load_recipients()
    html = _device_down_email_html(name, serial, site, st.get("type") or "", mins)
    for recip in recipients:
        email = recip.get("email")
        if not email:
            continue
        sent = _send_email(email, f"🔴 Device Offline — {name}", html)
        if sent:
            try:
                # Unique source_id per outage alert so the table's UNIQUE
                # constraint doesn't swallow future alerts for the same device.
                db.record_notification(
                    "device_down", f"{serial}@{now:%Y%m%d%H%M}",
                    int(rule.get("offline_minutes") or 5), email,
                    f"{name} offline {mins}m (rule #{rule.get('id')})",
                )
            except Exception as e:
                logger.warning("Could not record device_down notification for %s: %s", serial, e)
    logger.warning("ALERT: device %s (%s) offline %dm — rule #%s, %d recipient(s)",
                   name, serial, mins, rule.get("id"), len(recipients))


def _notify_recovery(st: dict, serial: str, now: datetime):
    name = st.get("name") or serial
    offline_since = st.get("offline_since")
    mins = max(1, int((now - offline_since).total_seconds() // 60)) if offline_since else 0
    body = f"{name} ({serial}) is back online" + (f" after ~{mins} min offline" if mins else "")
    _add_in_app_safe(f"Device recovered: {name}", body, "info",
                     device_serial=serial, url=f"/devices/{serial}")
    logger.info("RECOVERED: device %s (%s) back online after ~%dm", name, serial, mins)


def _maybe_alert_down(st: dict, serial: str, rules: list[dict], now: datetime):
    offline_since = st.get("offline_since")
    if not offline_since:
        return None  # baseline-offline device — no pending timer
    alerted_at = st.get("alerted_at")
    if alerted_at and alerted_at >= offline_since:
        return None  # already alerted for this outage
    dev = {"serial": serial, "name": st.get("name"), "site": st.get("site"), "type": st.get("type")}
    rule = _matching_rule(rules, dev)
    if rule is None:
        return None
    offline_min = max(1, min(1440, int(rule.get("offline_minutes") or 5)))
    cooldown_min = max(1, min(1440, int(rule.get("cooldown_minutes") or 60)))
    if (now - offline_since) < timedelta(minutes=offline_min):
        return None  # not down long enough yet
    if alerted_at and (now - alerted_at) < timedelta(minutes=cooldown_min):
        return None  # cooldown from a previous outage alert
    _fire_down_alert(st, serial, rule, offline_since, now)
    st["alerted_at"] = now
    st["_dirty"] = True
    return {"serial": serial, "event": "down_alert", "rule_id": rule.get("id")}


def run_device_status_check(devices: list[dict] | None = None,
                            now: datetime | None = None) -> list[dict]:
    """Periodic device up/down transition check (called by APScheduler).

    devices/now parameters are injectable for tests; in production the job
    fetches the fleet itself in a fresh event loop. All DB operations are best
    effort — in-memory transition tracking keeps working if Postgres is down.
    """
    global _state_seeded
    now = now or datetime.now(timezone.utc)

    if devices is None:
        devices = _fetch_devices_sync()
    if devices is None:
        logger.warning("Device status check aborted — device fetch failed")
        return []
    devices = _normalize_devices(devices)

    if not _state_seeded:
        _seed_baseline(devices, now)
        _state_seeded = True
        _persist_snapshot()
        logger.info("Device status baseline seeded (%d devices) — no alerts on first run",
                    len(_device_state))
        return []

    rules = _load_rules()
    events: list[dict] = []

    for dev in devices:
        # One unparseable device — or a transient DB error raised from inside an
        # alert — must not abort the sweep for the rest of the fleet.
        try:
            events.extend(_process_device(dev, rules, now))
        except Exception:
            logger.warning("Device status check: skipping one device", exc_info=True)

    _persist_snapshot()
    if events:
        logger.info("Device status check: %d event(s): %s", len(events), events)
    return events


def _process_device(dev: dict, rules: list[dict], now: datetime) -> list[dict]:
    """Advance one device's state machine. Returns any events it produced."""
    events: list[dict] = []
    serial = str(dev.get("serial") or "").strip()
    if not serial:
        return events
    name = dev.get("name") or serial
    online = _is_online(dev.get("status"))
    st = _device_state.get(serial)

    if st is None:
        # New device discovered mid-flight — baseline it without alerting.
        _device_state[serial] = _new_state(dev, online, now if not online else None)
        _record_transition_safe(serial, name, "online" if online else "offline")
        return events

    st["name"] = name
    st["site"] = dev.get("site") or ""
    st["type"] = _norm_device_type(dev.get("type"))

    if st["online"] and not online:
        # online -> offline: start the pending-down timer.
        st["online"] = False
        st["offline_since"] = now
        st["_dirty"] = True
        _record_transition_safe(serial, name, "offline")
        events.append({"serial": serial, "event": "went_offline"})
    elif not st["online"] and online:
        # offline -> online: recovery notice only if we actually alerted.
        alerted_for_outage = (
            st.get("alerted_at") and st.get("offline_since")
            and st["alerted_at"] >= st["offline_since"]
        )
        if alerted_for_outage:
            _notify_recovery(st, serial, now)
            events.append({"serial": serial, "event": "recovered"})
        st["online"] = True
        st["offline_since"] = None
        st["_dirty"] = True
        _record_transition_safe(serial, name, "online")
    elif not online:
        # Still offline — alert once threshold is met (cooldown-aware).
        ev = _maybe_alert_down(st, serial, rules, now)
        if ev:
            events.append(ev)
    return events


def _device_down_email_html(name: str, serial: str, site: str, dtype: str, mins: int) -> str:
    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:24px;">
        <h2 style="color:#ef4444;margin:0 0 16px;">🔴 Device Offline</h2>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <tr><td style="padding:8px;color:#666;width:140px;">Device</td>
                <td style="padding:8px;font-weight:bold;">{name}</td></tr>
            <tr><td style="padding:8px;color:#666;">Serial</td>
                <td style="padding:8px;font-family:monospace;">{serial}</td></tr>
            <tr><td style="padding:8px;color:#666;">Type</td>
                <td style="padding:8px;">{dtype or '—'}</td></tr>
            <tr><td style="padding:8px;color:#666;">Site</td>
                <td style="padding:8px;">{site or '—'}</td></tr>
            <tr><td style="padding:8px;color:#666;">Offline For</td>
                <td style="padding:8px;font-weight:bold;color:#ef4444;">{mins} minutes</td></tr>
        </table>
        <p style="margin:20px 0 0;padding:12px;background:#fee2e2;border-radius:8px;color:#991b1b;font-size:13px;">
            Automated device-down alert from New Central Portal. You will not be
            re-alerted for this device until it recovers or the cooldown elapses.
        </p>
    </div>
    """


# ═════════════════════════════════════════════════════════════════════════════
# Scheduled summary reports
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_subscriptions_sync() -> list[dict]:
    try:
        from vendors.central_bridge import get_glp_subscriptions
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            subs = loop.run_until_complete(get_glp_subscriptions())
        finally:
            loop.close()
        return subs if isinstance(subs, list) else []
    except Exception as e:
        logger.warning("Summary report: could not fetch GLP subscriptions: %s", e)
        return []


def run_summary_report(force: bool = False, devices: list[dict] | None = None,
                       subs: list[dict] | None = None,
                       now: datetime | None = None) -> dict:
    """Hourly-scheduled summary report job.

    Sends only when enabled, the configured hour (UTC) matches, the frequency
    window (daily/weekly) is due and it wasn't already sent in this window.
    force=True (test button) bypasses the schedule checks. Async callers must
    pass pre-fetched devices/subs to avoid nested event loops.
    """
    now = now or datetime.now(timezone.utc)
    try:
        cfg = db.get_report_settings()
    except Exception as e:
        logger.error("Summary report: settings unavailable: %s", e)
        return {"ok": False, "error": "Database unavailable"}

    if not force:
        if not cfg.get("enabled"):
            return {"ok": True, "skipped": "disabled"}
        try:
            cfg_hour = int(cfg.get("hour") if cfg.get("hour") is not None else 8)
        except (TypeError, ValueError):
            cfg_hour = 8
        if cfg_hour != now.hour:
            return {"ok": True, "skipped": "hour mismatch"}
        freq = str(cfg.get("frequency") or "daily").lower()
        if freq == "weekly" and now.weekday() != 0:  # Mondays
            return {"ok": True, "skipped": "weekday"}
        last = cfg.get("last_sent")
        if last is not None and hasattr(last, "tzinfo"):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            min_gap = timedelta(hours=20) if freq == "daily" else timedelta(days=6)
            if now - last < min_gap:
                return {"ok": True, "skipped": "already sent in window"}

    try:
        recipients = db.get_recipients()
    except Exception as e:
        logger.error("Summary report: recipients unavailable: %s", e)
        return {"ok": False, "error": "Database unavailable"}
    if not recipients:
        logger.info("Summary report: no recipients configured — skipping")
        return {"ok": False, "error": "No recipients configured"}

    if devices is None:
        devices = _fetch_devices_sync() or []
    # Raw when injected by /notifications/reports/test, raw when self-fetched —
    # normalize here or every row renders "?" / blank serial / "unknown" type.
    devices = _normalize_devices(devices)
    if subs is None:
        subs = _fetch_subscriptions_sync()

    html = _summary_report_html(devices, subs, now)
    subject = f"📊 Fleet Summary Report — {now:%Y-%m-%d}"
    sent = 0
    for recip in recipients:
        email = recip.get("email")
        if not email:
            continue
        if _send_email(email, subject, html):
            sent += 1
            try:
                db.record_notification("summary_report", f"{now:%Y%m%d%H}", 0, email,
                                       f"{len(devices)} devices")
            except Exception:
                logger.warning("Could not record summary report send", exc_info=True)
    if sent:
        try:
            db.mark_report_sent(now)
        except Exception:
            logger.warning("Could not update report last_sent", exc_info=True)
    logger.info("Summary report: sent to %d/%d recipient(s)", sent, len(recipients))
    if sent:
        return {"ok": True, "sent": sent, "recipients": len(recipients)}
    return {"ok": False, "sent": 0, "recipients": len(recipients),
            "error": "SMTP send failed — check SMTP settings and server logs"}


def _summary_report_html(devices: list[dict], subs: list[dict], now: datetime) -> str:
    # Fleet summary by type
    by_type: dict[str, dict] = {}
    offline_devices = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        t = _norm_device_type(d.get("type"))
        b = by_type.setdefault(t, {"total": 0, "online": 0, "offline": 0})
        b["total"] += 1
        if _is_online(d.get("status")):
            b["online"] += 1
        else:
            b["offline"] += 1
            offline_devices.append(d)
    total = sum(b["total"] for b in by_type.values())
    online = sum(b["online"] for b in by_type.values())
    offline = total - online

    type_rows = ""
    for t in sorted(by_type):
        b = by_type[t]
        type_rows += (
            f"<tr style='border-bottom:1px solid #eee;'>"
            f"<td style='padding:8px;font-size:13px;'>{t}</td>"
            f"<td style='padding:8px;font-size:13px;'>{b['total']}</td>"
            f"<td style='padding:8px;font-size:13px;color:#16a34a;'>{b['online']}</td>"
            f"<td style='padding:8px;font-size:13px;color:#ef4444;'>{b['offline']}</td></tr>"
        )

    offline_rows = ""
    for d in offline_devices[:25]:
        offline_rows += (
            f"<tr style='border-bottom:1px solid #eee;'>"
            f"<td style='padding:8px;font-size:13px;'>{d.get('name') or d.get('serial') or '?'}</td>"
            f"<td style='padding:8px;font-family:monospace;font-size:12px;'>{d.get('serial') or ''}</td>"
            f"<td style='padding:8px;font-size:13px;'>{_norm_device_type(d.get('type'))}</td>"
            f"<td style='padding:8px;font-size:13px;'>{d.get('site') or '—'}</td></tr>"
        )
    offline_extra = (f"<p style='color:#888;font-size:12px;'>…and {len(offline_devices) - 25} more.</p>"
                     if len(offline_devices) > 25 else "")
    offline_section = (
        f"<h3 style='margin:24px 0 8px;color:#ef4444;font-size:15px;'>Devices Currently Offline ({len(offline_devices)})</h3>"
        f"<table style='border-collapse:collapse;width:100%;'><tbody>{offline_rows}</tbody></table>{offline_extra}"
        if offline_devices else
        "<p style='margin:24px 0 8px;color:#16a34a;font-size:14px;'>All devices online. ✓</p>"
    )

    # Recent alert activity (best effort)
    alerts_24h = alerts_7d = None
    try:
        alerts_24h = db.count_recent_alerts(24)
        alerts_7d = db.count_recent_alerts(24 * 7)
    except Exception as e:
        logger.warning("Summary report: could not count recent alerts: %s", e)
    alerts_section = (
        f"<p style='font-size:13px;color:#555;'>Alerts sent: <b>{alerts_24h}</b> in the last 24h, "
        f"<b>{alerts_7d}</b> in the last 7 days.</p>"
        if alerts_24h is not None else ""
    )

    # Expiring subscriptions ≤ 90 days (cheap reuse of pre-fetched GLP data)
    expiring_rows = ""
    n_expiring = 0
    for s in subs or []:
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
            n_expiring += 1
            if n_expiring <= 15:
                color = "#ef4444" if days_left <= 15 else "#f59e0b" if days_left <= 30 else "#fb923c"
                expiring_rows += (
                    f"<tr style='border-bottom:1px solid #eee;'>"
                    f"<td style='padding:8px;font-family:monospace;font-size:12px;'>{s.get('key', '')}</td>"
                    f"<td style='padding:8px;font-size:13px;'>{s.get('tier') or s.get('subscriptionType') or ''}</td>"
                    f"<td style='padding:8px;font-size:13px;'>{end_str[:10]}</td>"
                    f"<td style='padding:8px;font-weight:bold;color:{color};font-size:13px;'>{days_left}d</td></tr>"
                )
    expiry_section = (
        f"<h3 style='margin:24px 0 8px;color:#f59e0b;font-size:15px;'>Subscriptions Expiring ≤ 90 Days ({n_expiring})</h3>"
        f"<table style='border-collapse:collapse;width:100%;'><tbody>{expiring_rows}</tbody></table>"
        if n_expiring else ""
    )

    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:700px;margin:0 auto;padding:24px;">
        <h2 style="color:#f97316;margin:0 0 4px;">📊 Fleet Summary Report</h2>
        <p style="color:#666;font-size:13px;margin:0 0 20px;">{now.strftime('%Y-%m-%d %H:%M UTC')} — New Central Portal</p>
        <p style="font-size:14px;color:#333;">
            <b>{total}</b> devices — <b style="color:#16a34a;">{online} online</b>,
            <b style="color:#ef4444;">{offline} offline</b>.
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:14px;">
            <thead><tr style="background:#f8f8f8;border-bottom:2px solid #ddd;">
                <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Type</th>
                <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Total</th>
                <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Online</th>
                <th style="padding:8px;text-align:left;font-size:12px;text-transform:uppercase;color:#888;">Offline</th>
            </tr></thead>
            <tbody>{type_rows}</tbody>
        </table>
        {offline_section}
        {alerts_section}
        {expiry_section}
        <p style="margin:24px 0 0;padding:12px;background:#f1f5f9;border-radius:8px;color:#475569;font-size:12px;">
            Automated summary report from New Central Portal.
        </p>
    </div>
    """
