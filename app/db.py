"""Lightweight DB helpers using psycopg2 + connection pooling.

Importing this module never opens a connection — the pool is created
lazily on first use (get_pool), so the app can start without Postgres.
"""
import logging
import os
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_pool: pool.SimpleConnectionPool | None = None

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://netlab:netlab@db:5432/netlab"
)


def _parse_dsn(url: str) -> dict:
    from urllib.parse import urlparse
    p = urlparse(url)
    return {
        "host": p.hostname or "db",
        "port": p.port or 5432,
        "dbname": p.path.lstrip("/") or "netlab",
        "user": p.username or "netlab",
        "password": p.password or "netlab",
    }


def get_pool() -> pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        try:
            _pool = pool.SimpleConnectionPool(1, 5, **_parse_dsn(DATABASE_URL))
        except Exception:
            logger.exception("Failed to create database connection pool")
            raise
    return _pool


def close_pool() -> None:
    """Close all pooled connections (call on app shutdown)."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
            logger.info("Database connection pool closed")
        except Exception:
            logger.exception("Error closing database connection pool")
        finally:
            _pool = None


@contextmanager
def get_conn():
    p = get_pool()
    conn = p.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            logger.warning("Rollback failed on pooled connection", exc_info=True)
        raise
    finally:
        try:
            p.putconn(conn, close=conn.closed != 0)
        except Exception:
            logger.warning("Failed to return connection to pool", exc_info=True)


def ping() -> bool:
    """Cheap connectivity check (used by /healthz). Never raises."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception as exc:
        logger.debug("Database ping failed: %s", exc)
        return False


def execute(sql: str, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def fetchone(sql: str, params=None) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def fetchall(sql: str, params=None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alert_settings (
    id          SERIAL PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_recipients (
    id          SERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notifications_sent (
    id          SERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,          -- 'subscription' or 'ssl_cert'
    source_id   TEXT NOT NULL,          -- subscription key or hostname
    threshold   INTEGER NOT NULL,       -- 90, 60, 30, or 15
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    recipient   TEXT NOT NULL,
    details     TEXT,
    UNIQUE(source_type, source_id, threshold, recipient)
);

-- Device-down alerting ------------------------------------------------------

CREATE TABLE IF NOT EXISTS device_status_history (
    id          SERIAL PRIMARY KEY,
    serial      TEXT NOT NULL,
    name        TEXT,
    status      TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS device_status_snapshot (
    serial        TEXT PRIMARY KEY,
    name          TEXT,
    status        TEXT NOT NULL,
    offline_since TIMESTAMPTZ,
    alerted_at    TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id                 SERIAL PRIMARY KEY,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    site_filter        TEXT,
    device_type_filter TEXT,
    offline_minutes    INTEGER NOT NULL DEFAULT 5,
    cooldown_minutes   INTEGER NOT NULL DEFAULT 60,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS in_app_notifications (
    id            SERIAL PRIMARY KEY,
    title         TEXT NOT NULL,
    body          TEXT,
    severity      TEXT NOT NULL DEFAULT 'info',
    device_serial TEXT,
    url           TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read          BOOLEAN NOT NULL DEFAULT FALSE
);

-- Scheduled summary reports (single-row settings table) ---------------------

CREATE TABLE IF NOT EXISTS report_settings (
    id        SERIAL PRIMARY KEY,
    enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    frequency TEXT NOT NULL DEFAULT 'daily',
    hour      INTEGER NOT NULL DEFAULT 8,
    last_sent TIMESTAMPTZ
);
"""


def init_db():
    """Create schema and seed defaults. Logs and re-raises on failure so the
    caller can decide whether startup should continue degraded."""
    try:
        execute(SCHEMA_SQL)
    except Exception:
        logger.exception("Database schema initialisation failed")
        raise
    # Seed default settings if empty
    defaults = {
        "thresholds": "90,60,30,15",
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_password": "",
        "smtp_from": "",
        "smtp_tls": "true",
        "check_subscriptions": "true",
        "check_ssl": "true",
        "ssl_hosts": "",
    }
    try:
        for k, v in defaults.items():
            execute(
                "INSERT INTO alert_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v),
            )
    except Exception:
        logger.exception("Failed to seed default alert settings")
        raise
    # Seed one default device-down alert rule on first init only (guarded by a
    # settings flag so deleting all rules later doesn't resurrect it on restart).
    try:
        if get_setting("alert_rules_seeded") != "true":
            row = fetchone("SELECT COUNT(*) AS n FROM alert_rules")
            if row and int(row["n"]) == 0:
                execute(
                    "INSERT INTO alert_rules (enabled, site_filter, device_type_filter, offline_minutes, cooldown_minutes) "
                    "VALUES (TRUE, NULL, NULL, 5, 60)"
                )
            set_setting("alert_rules_seeded", "true")
    except Exception:
        logger.exception("Failed to seed default alert rule")
    # Seed the single report_settings row.
    try:
        execute(
            "INSERT INTO report_settings (id, enabled, frequency, hour) VALUES (1, FALSE, 'daily', 8) "
            "ON CONFLICT (id) DO NOTHING"
        )
    except Exception:
        logger.exception("Failed to seed report settings")
    logger.info("Database schema initialised")


def get_setting(key: str) -> str:
    row = fetchone("SELECT value FROM alert_settings WHERE key = %s", (key,))
    return row["value"] if row else ""


def set_setting(key: str, value: str):
    execute(
        "INSERT INTO alert_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def get_recipients() -> list[dict]:
    rows = fetchall("SELECT * FROM alert_recipients WHERE active = TRUE ORDER BY email")
    for r in rows:
        if hasattr(r.get("created_at"), "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
    return rows


def add_recipient(email: str):
    execute(
        "INSERT INTO alert_recipients (email) VALUES (%s) ON CONFLICT (email) DO UPDATE SET active = TRUE",
        (email,),
    )


def remove_recipient(email: str):
    execute("UPDATE alert_recipients SET active = FALSE WHERE email = %s", (email,))


def was_notified(source_type: str, source_id: str, threshold: int, recipient: str) -> bool:
    row = fetchone(
        "SELECT id FROM notifications_sent WHERE source_type=%s AND source_id=%s AND threshold=%s AND recipient=%s",
        (source_type, source_id, threshold, recipient),
    )
    return row is not None


def record_notification(source_type: str, source_id: str, threshold: int, recipient: str, details: str = ""):
    execute(
        "INSERT INTO notifications_sent (source_type, source_id, threshold, recipient, details) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (source_type, source_id, threshold, recipient, details),
    )


def get_notification_history(limit: int = 100) -> list[dict]:
    rows = fetchall(
        "SELECT * FROM notifications_sent ORDER BY sent_at DESC LIMIT %s", (limit,)
    )
    for r in rows:
        if hasattr(r.get("sent_at"), "isoformat"):
            r["sent_at"] = r["sent_at"].isoformat()
    return rows


# ── Alert rules (device-down) ────────────────────────────────────────────────

def get_alert_rules(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM alert_rules"
    if enabled_only:
        sql += " WHERE enabled = TRUE"
    sql += " ORDER BY id"
    rows = fetchall(sql)
    for r in rows:
        if hasattr(r.get("created_at"), "isoformat"):
            r["created_at"] = r["created_at"].isoformat()
    return rows


def add_alert_rule(site_filter: str | None, device_type_filter: str | None,
                   offline_minutes: int, cooldown_minutes: int) -> dict | None:
    row = fetchone(
        "INSERT INTO alert_rules (site_filter, device_type_filter, offline_minutes, cooldown_minutes) "
        "VALUES (%s, %s, %s, %s) RETURNING *",
        (site_filter, device_type_filter, offline_minutes, cooldown_minutes),
    )
    if row and hasattr(row.get("created_at"), "isoformat"):
        row["created_at"] = row["created_at"].isoformat()
    return row


def update_alert_rule(rule_id: int, fields: dict) -> bool:
    """Update allowed columns of a rule. Returns False if the rule is missing."""
    allowed = ("enabled", "site_filter", "device_type_filter", "offline_minutes", "cooldown_minutes")
    sets, params = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k} = %s")
            params.append(fields[k])
    if not sets:
        return fetchone("SELECT id FROM alert_rules WHERE id = %s", (rule_id,)) is not None
    params.append(rule_id)
    row = fetchone(
        f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = %s RETURNING id", params
    )
    return row is not None


def delete_alert_rule(rule_id: int) -> bool:
    row = fetchone("DELETE FROM alert_rules WHERE id = %s RETURNING id", (rule_id,))
    return row is not None


# ── In-app notifications ─────────────────────────────────────────────────────

def add_in_app_notification(title: str, body: str = "", severity: str = "info",
                            device_serial: str | None = None, url: str | None = None):
    execute(
        "INSERT INTO in_app_notifications (title, body, severity, device_serial, url) "
        "VALUES (%s, %s, %s, %s, %s)",
        (title, body, severity, device_serial, url),
    )


def get_in_app_notifications(limit: int = 15) -> list[dict]:
    return fetchall(
        "SELECT * FROM in_app_notifications ORDER BY created_at DESC, id DESC LIMIT %s",
        (limit,),
    )


def count_unread_notifications() -> int:
    row = fetchone("SELECT COUNT(*) AS n FROM in_app_notifications WHERE read = FALSE")
    return int(row["n"]) if row else 0


def mark_notifications_read(ids: list[int] | None = None, mark_all: bool = False) -> int:
    """Mark notifications read; returns the remaining unread count."""
    if mark_all:
        execute("UPDATE in_app_notifications SET read = TRUE WHERE read = FALSE")
    elif ids:
        execute("UPDATE in_app_notifications SET read = TRUE WHERE id = ANY(%s)", (ids,))
    return count_unread_notifications()


# ── Device status snapshot / history ─────────────────────────────────────────

def load_device_snapshot() -> dict[str, dict]:
    """Return {serial: {status, name, offline_since, alerted_at}} for restart-safe state."""
    rows = fetchall("SELECT * FROM device_status_snapshot")
    return {r["serial"]: r for r in rows}


def upsert_device_snapshot(serial: str, name: str | None, status: str,
                           offline_since=None, alerted_at=None):
    execute(
        "INSERT INTO device_status_snapshot (serial, name, status, offline_since, alerted_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW()) "
        "ON CONFLICT (serial) DO UPDATE SET name = EXCLUDED.name, status = EXCLUDED.status, "
        "offline_since = EXCLUDED.offline_since, alerted_at = EXCLUDED.alerted_at, updated_at = NOW()",
        (serial, name, status, offline_since, alerted_at),
    )


def record_status_transition(serial: str, name: str | None, status: str):
    execute(
        "INSERT INTO device_status_history (serial, name, status) VALUES (%s, %s, %s)",
        (serial, name, status),
    )


def count_recent_alerts(hours: int = 24) -> int:
    row = fetchone(
        "SELECT COUNT(*) AS n FROM notifications_sent WHERE sent_at > NOW() - (%s * INTERVAL '1 hour')",
        (hours,),
    )
    return int(row["n"]) if row else 0


# ── Summary report settings (single-row) ─────────────────────────────────────

def get_report_settings() -> dict:
    row = fetchone("SELECT * FROM report_settings ORDER BY id LIMIT 1")
    if row is None:
        row = fetchone(
            "INSERT INTO report_settings (id, enabled, frequency, hour) VALUES (1, FALSE, 'daily', 8) "
            "ON CONFLICT (id) DO NOTHING RETURNING *"
        ) or {"id": 1, "enabled": False, "frequency": "daily", "hour": 8, "last_sent": None}
    return row


def save_report_settings(enabled: bool | None = None, frequency: str | None = None,
                         hour: int | None = None):
    cfg = get_report_settings()
    rid = cfg.get("id", 1)
    sets, params = [], []
    if enabled is not None:
        sets.append("enabled = %s"); params.append(enabled)
    if frequency is not None:
        sets.append("frequency = %s"); params.append(frequency)
    if hour is not None:
        sets.append("hour = %s"); params.append(hour)
    if sets:
        params.append(rid)
        execute(f"UPDATE report_settings SET {', '.join(sets)} WHERE id = %s", params)


def mark_report_sent(when=None):
    cfg = get_report_settings()
    execute(
        "UPDATE report_settings SET last_sent = COALESCE(%s, NOW()) WHERE id = %s",
        (when, cfg.get("id", 1)),
    )
