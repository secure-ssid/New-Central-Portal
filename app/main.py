"""
New Central Portal - Network operations and tooling
Main FastAPI entry point.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from routes import home, devices, clients, sites, lab, topology
from routes import notifications as notifications_routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    import db
    db.init_db()

    # Daily expiry check scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from notifications import run_expiry_check
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_expiry_check, "cron", hour=7, minute=0, id="expiry_check")
    scheduler.start()

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)


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


@app.get("/health")
def health():
    """Quick liveness check."""
    return {"status": "ok"}
