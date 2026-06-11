"""
New Central Portal - Network operations and tooling
Main FastAPI entry point.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from routes import home, devices, clients, sites, lab, topology
from routes import assistant as assistant_routes
from routes import notifications as notifications_routes
from routes import search as search_routes

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
            from config import settings
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

# Static files (CSS, JS, images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Make templates available to routes
templates = Jinja2Templates(directory="templates")

# Wire up the main sections
app.include_router(home.router)
app.include_router(devices.router, prefix="/devices", tags=["devices"])
app.include_router(clients.router, prefix="/clients", tags=["clients"])
app.include_router(sites.router, prefix="/sites", tags=["sites"])
app.include_router(lab.router, prefix="/lab", tags=["lab"])
app.include_router(topology.router, prefix="/topology", tags=["topology"])
app.include_router(notifications_routes.router, prefix="/notifications", tags=["notifications"])
app.include_router(search_routes.router, prefix="/search", tags=["search"])
app.include_router(assistant_routes.router, prefix="/assistant", tags=["assistant"])


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
