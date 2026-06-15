"""Session-cookie auth primitives: HMAC-signed tokens, password checking,
login rate limiting, CSRF origin validation, and the audit-log helpers.

Implemented with the stdlib only (``hmac``/``hashlib``/``secrets``) —
``itsdangerous`` is intentionally NOT a dependency of this project.

Token format (cookie value)::

    v1.<expires_epoch>.<nonce>.<base64url(HMAC_SHA256(secret, payload))>

where ``payload`` is the first three dot-joined fields. Verification is
constant-time (``hmac.compare_digest``) and checks expiry.
"""
import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from urllib.parse import urlsplit

from config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "portal_session"

# Fallback signing secret when SESSION_SECRET isn't configured. Generated
# fresh per process, so every restart invalidates all sessions (a warning is
# logged from config.validate_settings at startup).
_EPHEMERAL_SECRET = secrets.token_urlsafe(32)

_TOKEN_VERSION = "v1"
_MAX_TOKEN_LEN = 512


# ── Auth state ────────────────────────────────────────────────────────────────

def auth_enabled() -> bool:
    """Auth is enabled iff a portal password is configured — either the env
    bootstrap password or a hash saved via the Change Password UI."""
    return bool(settings.portal_password) or bool(_db_password_hash())


def _secret_bytes() -> bytes:
    return (settings.session_secret or _EPHEMERAL_SECRET).encode("utf-8")


# ── Session tokens ────────────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    digest = hmac.new(_secret_bytes(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def session_max_age_seconds() -> int:
    try:
        hours = int(settings.session_max_age_hours)
    except (TypeError, ValueError):
        hours = 24
    return max(1, hours) * 3600


def create_session_token() -> str:
    """Mint a signed session token that expires after session_max_age_hours."""
    expires = int(time.time()) + session_max_age_seconds()
    nonce = secrets.token_urlsafe(8)
    payload = f"{_TOKEN_VERSION}.{expires}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def verify_session_token(token) -> bool:
    """True iff the token has a valid signature (constant-time compare) and
    has not expired. Never raises on malformed input."""
    if not token or not isinstance(token, str) or len(token) > _MAX_TOKEN_LEN:
        return False
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_VERSION:
        return False
    payload = ".".join(parts[:3])
    if not hmac.compare_digest(_sign(payload), parts[3]):
        return False
    try:
        expires = int(parts[1])
    except ValueError:
        return False
    return time.time() < expires


# Password storage: a hash saved in the DB (via the Change Password UI) takes
# precedence over the PORTAL_PASSWORD env bootstrap value. Clearing the DB
# setting (set it to "") reverts authentication to the env password.
_PBKDF2_ITERATIONS = 240_000


def hash_password(pw: str) -> str:
    """Salted PBKDF2-SHA256 hash, formatted as algo$iterations$salt$hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_pbkdf2(submitted: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", submitted.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _db_password_hash():
    """The DB-stored portal password hash, or None if unset/unavailable."""
    try:
        import db
        v = (db.get_setting("portal_password_hash") or "").strip()
        return v or None
    except Exception:
        return None


def set_portal_password(pw: str) -> None:
    """Persist a new portal password (hashed) in the DB, superseding env."""
    import db
    db.set_setting("portal_password_hash", hash_password(pw))


def verify_password(submitted) -> bool:
    """Check a submitted password against the DB hash if one is set, else the
    env bootstrap password. Constant-time; never raises on bad input."""
    if not isinstance(submitted, str):
        return False
    stored = _db_password_hash()
    if stored:
        return _verify_pbkdf2(submitted, stored)
    if not settings.portal_password:
        return False
    return secrets.compare_digest(
        submitted.encode("utf-8"), settings.portal_password.encode("utf-8")
    )


# ── Request helpers ───────────────────────────────────────────────────────────

def client_ip(request) -> str:
    """Best client IP: first X-Forwarded-For entry (Caddy sets it), else the
    socket peer address."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def is_secure_request(request) -> bool:
    """True when the original client connection used HTTPS. Caddy terminates
    TLS in front of the app, so honor X-Forwarded-Proto when present."""
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def sanitize_next(raw) -> str:
    """Validate a post-login redirect target. Only same-origin absolute paths
    are allowed: must start with a single '/', no '//', no backslashes, no
    scheme, no CR/LF. Anything else collapses to '/'."""
    if not raw or not isinstance(raw, str):
        return "/"
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if "\\" in raw or "\r" in raw or "\n" in raw:
        return "/"
    # Defense in depth: a parseable netloc or scheme means it isn't a plain path.
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return raw


def check_csrf(request) -> tuple[bool, str]:
    """Same-origin check for unsafe methods: the Origin header (or Referer as
    fallback) must match the request Host. A browser request carrying neither
    header is rejected. Non-browser clients (curl, scripts) without either
    header are allowed — cross-site cookie riding requires a browser.

    Returns (ok, reason); reason is set when ok is False.
    """
    host = (request.headers.get("host") or "").strip().lower()
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        if origin.lower() == "null":
            return False, "Origin header is 'null'"
        netloc = urlsplit(origin).netloc.lower()
        if netloc and host and netloc == host:
            return True, ""
        return False, f"Origin {origin!r} does not match host {host!r}"
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        netloc = urlsplit(referer).netloc.lower()
        if netloc and host and netloc == host:
            return True, ""
        return False, f"Referer {referer!r} does not match host {host!r}"
    ua = (request.headers.get("user-agent") or "").lower()
    if "mozilla" in ua or "sec-fetch-site" in request.headers:
        return False, "Browser request missing both Origin and Referer"
    return True, ""


# ── Login rate limiting (in-memory, per IP) ──────────────────────────────────

class LoginRateLimiter:
    """Sliding-window failed-attempt limiter. In-memory and per-process,
    which is fine for a single-instance portal."""

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        kept = [t for t in self._attempts.get(ip, []) if t > cutoff]
        if kept:
            self._attempts[ip] = kept
        else:
            self._attempts.pop(ip, None)
        return kept

    def is_limited(self, ip: str) -> bool:
        with self._lock:
            return len(self._prune(ip, time.time())) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = time.time()
            self._prune(ip, now)
            self._attempts.setdefault(ip, []).append(now)
            # Cap memory: keep at most max_attempts entries per IP.
            self._attempts[ip] = self._attempts[ip][-self.max_attempts:]

    def reset(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)


login_limiter = LoginRateLimiter(max_attempts=10, window_seconds=300)


# ── Audit log ────────────────────────────────────────────────────────────────

AUDIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    client_ip   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def ensure_audit_schema() -> None:
    """Create the audit_log table if missing. Never raises — a down DB at
    startup just logs a warning (insert attempts later are also best-effort)."""
    try:
        import db
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(AUDIT_SCHEMA_SQL)
        logger.info("Audit-log schema ensured")
    except Exception as exc:
        logger.warning("Could not ensure audit_log schema (DB unavailable?): %s", exc)


def record_audit(method: str, path: str, ip: str) -> None:
    """Best-effort insert of one audit row; swallows and logs DB errors."""
    try:
        import db
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (method, path, client_ip) VALUES (%s, %s, %s)",
                    (method[:16], path[:2000], (ip or "")[:100]),
                )
    except Exception as exc:
        logger.warning("Audit insert failed for %s %s: %s", method, path, exc)
