"""
New Central Portal - Network operations and tooling
Main FastAPI entry point.
"""
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import security
from config import settings
from routes import home, devices, clients, sites, lab, topology
from routes import assistant as assistant_routes
from routes import auth as auth_routes
from routes import notifications as notifications_routes
from routes import search as search_routes
from routes import status as status_routes
from routes import alerts as alerts_routes
from routes import wlans as wlans_routes
from routes import platform as platform_routes

# Logging: configure once, but don't stomp on uvicorn's handlers if present.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
else:
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    from config import validate_settings
    validate_settings()

    import db
    try:
        db.init_db()
    except Exception:
        # Logged inside init_db; start degraded — /healthz will report db: fail.
        logger.error("Database init failed — continuing without DB (degraded mode)")

    # Audit-log table (best-effort; never blocks startup if the DB is down).
    try:
        security.ensure_audit_schema()
    except Exception:
        logger.exception("Audit-log schema setup failed — continuing")

    if settings.portal_password:
        logger.info("Authentication ENABLED — login required at /login")
    else:
        logger.warning(
            "Authentication DISABLED (PORTAL_PASSWORD empty) — the portal is "
            "open to anyone who can reach it"
        )

    # Background job scheduler (expiry check + device-down alerts + reports)
    scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from notifications import run_expiry_check
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_expiry_check, "cron", hour=7, minute=0, id="expiry_check")
        scheduler.start()
        logger.info("Expiry-check scheduler started (daily 07:00)")
    except Exception:
        logger.exception("Failed to start expiry-check scheduler")

    if scheduler is not None:
        # Device-down alert engine — every 60s (configurable).
        try:
            from notifications import run_device_status_check
            interval = max(15, int(settings.device_check_interval_seconds or 60))
            scheduler.add_job(
                run_device_status_check, "interval", seconds=interval,
                id="device_status_check", max_instances=1, coalesce=True,
            )
            logger.info("Device-status check job registered (every %ss)", interval)
        except Exception:
            logger.exception("Failed to register device-status check job")
        # Scheduled summary reports — hourly; the job itself decides whether
        # the configured hour/frequency window is due.
        try:
            from notifications import run_summary_report
            scheduler.add_job(
                run_summary_report, "cron", minute=5,
                id="summary_report", max_instances=1, coalesce=True,
            )
            logger.info("Summary-report job registered (hourly at :05)")
        except Exception:
            logger.exception("Failed to register summary-report job")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Error shutting down scheduler")
    try:
        db.close_pool()
    except Exception:
        logger.exception("Error closing database pool")


app = FastAPI(title="New Central Portal", lifespan=lifespan)


# ── Session auth middleware ───────────────────────────────────────────────────
# Enforced on every request except the exempt paths below. Disabled entirely
# (pass-through) when PORTAL_PASSWORD is empty. CSRF strategy: SameSite=Lax
# session cookie + Origin/Referer same-host check on unsafe methods — no
# per-form tokens needed, so existing templates/HTMX markup stay untouched.

AUTH_EXEMPT_PATHS = {"/login", "/health", "/healthz", "/api/status", "/favicon.ico", "/auth/whoami"}
AUTH_EXEMPT_PREFIXES = ("/static/",)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Noisy endpoints excluded from the audit log (bell polling / chat traffic).
AUDIT_SKIP_PREFIXES = ("/notifications/api/", "/assistant/chat")


def _wants_json(request: Request) -> bool:
    """API/HTMX callers get 401 JSON instead of a login redirect."""
    if "hx-request" in request.headers:
        return True
    return "application/json" in request.headers.get("accept", "").lower()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Auth disabled — pass everything through (warned loudly at startup).
    if not settings.portal_password:
        return await call_next(request)

    path = request.url.path
    if path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    token = request.cookies.get(security.SESSION_COOKIE)
    if not security.verify_session_token(token):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": "Authentication required"},
                status_code=401,
                headers={"HX-Redirect": "/login"},
            )
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(
            f"/login?{urlencode({'next': security.sanitize_next(target)})}",
            status_code=303,
        )

    if request.method in UNSAFE_METHODS:
        ok, reason = security.check_csrf(request)
        if not ok:
            logger.warning(
                "CSRF rejection for %s %s from %s: %s",
                request.method, path, security.client_ip(request), reason,
            )
            if _wants_json(request):
                return JSONResponse(
                    {"ok": False, "error": "Cross-origin request rejected"},
                    status_code=403,
                )
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        # Audit trail for state-changing requests (best-effort, off-thread).
        if not path.startswith(AUDIT_SKIP_PREFIXES):
            try:
                await run_in_threadpool(
                    security.record_audit, request.method, path, security.client_ip(request)
                )
            except Exception:
                logger.debug("Audit record failed", exc_info=True)

    return await call_next(request)


# Static files (CSS, JS, images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Themed 404/500 pages (JSON for HTMX/API requests)
from errors import register_error_handlers  # noqa: E402

register_error_handlers(app)

# Wire up the main sections
app.include_router(auth_routes.router, tags=["auth"])
app.include_router(home.router)
app.include_router(devices.router, prefix="/devices", tags=["devices"])
app.include_router(clients.router, prefix="/clients", tags=["clients"])
app.include_router(sites.router, prefix="/sites", tags=["sites"])
app.include_router(lab.router, prefix="/lab", tags=["lab"])
app.include_router(topology.router, prefix="/topology", tags=["topology"])
app.include_router(notifications_routes.router, prefix="/notifications", tags=["notifications"])
app.include_router(search_routes.router, prefix="/search", tags=["search"])
app.include_router(assistant_routes.router, prefix="/assistant", tags=["assistant"])
app.include_router(status_routes.router, tags=["status"])
app.include_router(alerts_routes.router, prefix="/alerts", tags=["alerts"])
app.include_router(wlans_routes.router, prefix="/wlans", tags=["wlans"])
app.include_router(platform_routes.router, prefix="/platform", tags=["platform"])


@app.get("/health")
def health():
    """Quick liveness check."""
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    """Liveness + cheap dependency check (non-fatal if the DB is down)."""
    import db
    db_ok = db.ping()
    return {"status": "ok", "db": "ok" if db_ok else "fail"}
