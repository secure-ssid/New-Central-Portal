"""
New Central Portal - Network operations and tooling
Main FastAPI entry point.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

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


def _tame_centralmcp_rate_limit_backoff() -> None:
    """Shrink centralmcp's 429 backoff floor from 60s to 5s.

    Central never sends a Retry-After header, so centralmcp's floor is what
    every rate-limited call actually costs — and it sleeps *blocking* in a
    thread-pool worker. With the old 60s value a single 429 froze a page load
    for a full minute; loads were measured at 3s, 63s and 124s as they stacked.

    centralmcp is mounted read-only and upstream force-pushes main, so the
    matching fix in pipeline/clients/central_client.py can vanish on any
    update. This override reapplies it at startup. The constant is read at
    call time, so rebinding the module attribute is sufficient.
    """
    target = float(os.environ.get("CENTRAL_RATE_LIMIT_INITIAL_DELAY", "5"))
    try:
        from pipeline.clients import central_client
    except Exception:
        logger.debug("centralmcp central_client not importable — backoff override skipped")
        return
    current = getattr(central_client, "_INITIAL_RETRY_DELAY", None)
    if current is None:
        logger.warning(
            "centralmcp central_client._INITIAL_RETRY_DELAY is gone — "
            "429 backoff override no longer applies, check upstream"
        )
        return
    if current > target:
        central_client._INITIAL_RETRY_DELAY = target
        logger.info("Central 429 backoff floor lowered %ss → %ss", current, target)


# Set once a worker owns the background jobs, so /healthz can say so and an
# operator can tell "another worker has it" from "nobody has it".
_scheduler_state = {"role": "starting", "scheduler": None}

SCHEDULER_ELECTION_RETRY_SECONDS = 60


def _start_scheduler_if_leader():
    """Contend for the scheduler lock; start the jobs only if we win.

    Returns the BackgroundScheduler, or None if another worker owns the jobs or
    the database could not be reached.
    """
    import db  # module-level import would run before config validation

    outcome = db.try_acquire_scheduler_lock()
    if outcome == "held_by_peer":
        _scheduler_state["role"] = "follower"
        logger.info(
            "Background jobs are running in another worker — this one serves "
            "requests only"
        )
        return None
    if outcome != "acquired":
        # Distinct from the above on purpose: here NOBODY is running the jobs.
        _scheduler_state["role"] = "unelected"
        logger.warning(
            "Scheduler election could not reach the database — background jobs "
            "(device-down alerts, expiry checks, reports) are NOT running; "
            "retrying every %ss",
            SCHEDULER_ELECTION_RETRY_SECONDS,
        )
        return None

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from notifications import run_expiry_check
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_expiry_check, "cron", hour=7, minute=0, id="expiry_check")
        scheduler.start()
        logger.info("Expiry-check scheduler started (daily 07:00)")
    except Exception:
        logger.exception("Failed to start expiry-check scheduler")
        # Hand the lock back so another worker (or a later retry) can take over
        # rather than holding it while running nothing.
        db.drop_scheduler_lock_connection()
        _scheduler_state["role"] = "unelected"
        return None

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
    # Scheduled summary reports — hourly; the job itself decides whether the
    # configured hour/frequency window is due.
    try:
        from notifications import run_summary_report
        scheduler.add_job(
            run_summary_report, "cron", minute=5,
            id="summary_report", max_instances=1, coalesce=True,
        )
        logger.info("Summary-report job registered (hourly at :05)")
    except Exception:
        logger.exception("Failed to register summary-report job")

    _scheduler_state["role"] = "leader"
    _scheduler_state["scheduler"] = scheduler
    return scheduler


async def _retry_scheduler_election():
    """Keep contending until this worker or a peer is actually running the jobs."""
    while True:
        await asyncio.sleep(SCHEDULER_ELECTION_RETRY_SECONDS)
        try:
            scheduler = await run_in_threadpool(_start_scheduler_if_leader)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler election retry failed")
            continue
        if scheduler is not None:
            logger.info("Scheduler election succeeded on retry")
            return
        if _scheduler_state["role"] == "follower":
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    from config import validate_settings
    validate_settings()

    _tame_centralmcp_rate_limit_backoff()

    # Collapses the duplicate event fetches several centralmcp tools make for
    # the same device — /network-troubleshooting/v1/events is the endpoint
    # Central rate-limits first.
    try:
        from vendors.central_bridge import install_event_fetch_memo
        install_event_fetch_memo()
    except Exception:
        logger.exception("Event-fetch memo not installed — continuing")

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

    # Background job scheduler (expiry check + device-down alerts + reports).
    #
    # Exactly one worker runs these. The jobs send email and hit the Central
    # API, so running them per-worker would duplicate alerts and multiply the
    # upstream load. Election is a Postgres advisory lock — see
    # db.try_acquire_scheduler_lock.
    scheduler = _start_scheduler_if_leader()

    # Only "unelected" retries. A follower has a peer doing the work; an
    # unelected worker means the DB was unreachable and NOBODY is running the
    # jobs, which would otherwise persist for the life of the container.
    election_task = None
    if _scheduler_state["role"] == "unelected":
        election_task = asyncio.create_task(_retry_scheduler_election())

    # Fill this worker's Central cache in the background so the first visitor
    # does not pay for the cold fetch. Not awaited — the app serves immediately.
    warm_task = None
    try:
        from vendors.central_bridge import warm_cache
        warm_task = asyncio.create_task(warm_cache())
    except Exception:
        logger.debug("Cache warm not scheduled", exc_info=True)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    if election_task is not None:
        election_task.cancel()
    if warm_task is not None:
        warm_task.cancel()
    if scheduler is None:
        scheduler = _scheduler_state.get("scheduler")
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Error shutting down scheduler")
    try:
        db.release_scheduler_lock()
    except Exception:
        logger.exception("Error releasing scheduler lock")
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
    # Must consult auth_enabled(), not just the env var: the Change Password
    # UI stores a DB hash and login/verify honor it — gating only on
    # settings.portal_password would leave every route unauthenticated while
    # the login page still demands the GUI-set password. (Cached, ~10s TTL.)
    if not security.auth_enabled():
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


# ── No-store for HTML ─────────────────────────────────────────────────────────
# HTML pages embed the Alpine component scripts (command palette, assistant,
# notifications) INLINE, and carry no ETag/Last-Modified. Without an explicit
# directive a browser bfcache / service worker / edge proxy can replay a stale
# HTML body whose inline script predates a fix — which boots Alpine against
# mismatched markup and can leave the command palette frozen open (un-closeable).
# Stamping HTML no-store closes that vector. Static assets keep their
# ETag/Last-Modified validation untouched (they are not text/html).
#
# Defined AFTER auth_middleware so it is the OUTERMOST middleware and therefore
# stamps every HTML response, including the login page and auth redirects.
@app.middleware("http")
async def html_no_store_middleware(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Compression ───────────────────────────────────────────────────────────────
# Nothing compressed responses before this: the compose file ships a Caddy front
# end with `encode zstd gzip`, but it is disabled on the NAS (it would fight DSM
# for :80/:443), so traffic hits uvicorn directly. Pages are 89-210KB of HTML —
# base.html alone is 87KB, and the whole design system used to be inlined into
# every one of them. Added last so it is the outermost middleware and compresses
# the final body, including static assets.
app.add_middleware(GZipMiddleware, minimum_size=1000)


# Static files (CSS, JS, images)
#
# Plain StaticFiles emits ETag/Last-Modified but no Cache-Control, so every
# asset costs a conditional round trip on every page load — including the ~1.3MB
# three.js and force-graph bundles. Vendored files carry their version in the
# filename, so they can be cached indefinitely; generated and app-level assets
# get a short window plus revalidation so a rebuild is picked up promptly.
_IMMUTABLE_PREFIXES = ("vendor/", "fonts/", "icons/")
_SHORT_CACHE_SECONDS = 300


class CachedStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        path = scope.get("path", "").removeprefix("/static/")
        if path.startswith(_IMMUTABLE_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = (
                f"public, max-age={_SHORT_CACHE_SECONDS}, must-revalidate"
            )
        return response


app.mount("/static", CachedStaticFiles(directory="static"), name="static")

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
    """Liveness + cheap dependency check (non-fatal if the DB is down).

    ``scheduler`` reports this worker's role in the background-jobs election:
    leader (running them), follower (a peer is), or unelected (nobody is —
    alerting is down). Without it a portal with no scheduler at all looks
    perfectly healthy.
    """
    import db
    db_ok = db.ping()
    return {
        "status": "ok",
        "db": "ok" if db_ok else "fail",
        "scheduler": _scheduler_state["role"],
    }
