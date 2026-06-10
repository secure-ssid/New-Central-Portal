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
