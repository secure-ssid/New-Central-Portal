import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from vendors.aruba_central import aruba

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)

# Recent-events feed tuning: cap the fan-out so the dashboard stays cheap
# (and fast) even on large fleets — we only poll a handful of devices.
EVENT_FANOUT_DEVICES = 5
EVENT_FEED_LIMIT = 10
EVENT_LOOKBACK_HOURS = 24


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(part: int, whole: int) -> int:
    return int(round(part / whole * 100)) if whole else 0


def _parse_event_time(value) -> datetime | None:
    """Best-effort parse of Central event timestamps (ISO string or epoch)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            ts = float(value)
            if ts > 1e12:  # epoch milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _relative_age(ts: datetime | None, now: datetime) -> str:
    """Server-side '4m ago' display string for the events feed."""
    if ts is None:
        return ""
    secs = max(0, int((now - ts).total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _donut_segments(counts: list[tuple[int, str]], total: int) -> list[dict]:
    """SVG donut segments as stroke-dasharray/-offset pairs.

    Uses the classic r=15.9155 trick (circumference == 100) so dash lengths
    are plain percentages. Offset 25 starts the first segment at 12 o'clock;
    each later segment is shifted by what is already drawn.
    """
    if total <= 0:
        return []
    segments: list[dict] = []
    cumulative = 0.0
    for count, color in counts:
        if count <= 0:
            continue
        pct = count / total * 100.0
        segments.append({
            "color": color,
            "dash": f"{pct:.2f} {100.0 - pct:.2f}",
            "offset": f"{25.0 - cumulative:.2f}",
        })
        cumulative += pct
    return segments


async def _recent_events(devices: list[dict]) -> list[dict]:
    """Merge recent events from up to EVENT_FANOUT_DEVICES devices, newest first.

    Fully defensive: per-device failures are logged and skipped, and any
    unexpected error returns an empty feed so the dashboard never 500s.
    """
    try:
        from vendors.central_bridge import get_device_events

        candidates = [d for d in devices if isinstance(d, dict) and d.get("serial")]
        # Prefer online devices — they are the ones producing events.
        candidates.sort(key=lambda d: d.get("status") != "online")
        picks = candidates[:EVENT_FANOUT_DEVICES]
        if not picks:
            return []

        results = await asyncio.gather(
            *(
                get_device_events(d["serial"], hours=EVENT_LOOKBACK_HOURS, limit=EVENT_FEED_LIMIT)
                for d in picks
            ),
            return_exceptions=True,
        )

        now = datetime.now(timezone.utc)
        merged: list[dict] = []
        for dev, result in zip(picks, results):
            if isinstance(result, BaseException):
                logger.warning("Events unavailable for %s: %s", dev.get("serial"), result)
                continue
            for raw in result or []:
                if not isinstance(raw, dict):
                    continue
                ts = _parse_event_time(
                    raw.get("timeAt") or raw.get("time_at") or raw.get("timestamp") or raw.get("time")
                )
                text = str(raw.get("eventName") or raw.get("event_name") or raw.get("description") or "Event").strip() or "Event"
                detail = str(raw.get("description") or "").strip()
                merged.append({
                    "text": text,
                    "detail": "" if detail == text else detail,
                    "category": str(raw.get("category") or ""),
                    "device_name": dev.get("name") or dev.get("serial"),
                    "device_serial": dev.get("serial"),
                    "ago": _relative_age(ts, now),
                    "sort_ts": ts.timestamp() if ts else 0.0,
                })
        merged.sort(key=lambda e: e["sort_ts"], reverse=True)
        return merged[:EVENT_FEED_LIMIT]
    except Exception:
        logger.exception("Recent events feed unavailable")
        return []


def _render_live_fragment(request: Request, context: dict) -> HTMLResponse:
    """Render only the `dashboard_live` block of home.html (HTMX poll target).

    Same single-template fragment technique as jinja2-fragments: invoke the
    compiled block function directly so the full page and the 30s partial
    refresh can never drift apart.
    """
    template = templates.env.get_template("home.html")
    block = template.blocks["dashboard_live"]
    ctx = template.new_context({"request": request, **context})
    return HTMLResponse("".join(block(ctx)))


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/")
async def home(request: Request, partial: int = 0):
    """Dashboard / home page with quick stats.

    `?partial=1` returns only the `dashboard_live` fragment (no layout) —
    the page polls it via HTMX every 30s while the tab is visible.
    """
    devices, clients = await asyncio.gather(
        aruba.get_devices(),
        aruba.get_clients(),
        return_exceptions=True,
    )
    if isinstance(devices, BaseException):
        logger.warning("Devices unavailable for dashboard: %s", devices)
        devices = []
    if isinstance(clients, BaseException):
        logger.warning("Clients unavailable for dashboard: %s", clients)
        clients = []

    sites_count = 0
    try:
        from vendors.central_bridge import get_sites
        sites = await get_sites()
        sites_count = len(sites)
    except Exception as exc:
        logger.warning("Sites count unavailable for dashboard: %s", exc)

    total = len(devices)
    online = sum(1 for d in devices if d.get("status") == "online")
    offline_strict = sum(1 for d in devices if d.get("status") == "offline")
    unknown = total - online - offline_strict

    switches = sum(1 for d in devices if d.get("type") == "switch")
    aps = sum(1 for d in devices if d.get("type") == "access_point")
    gateways = sum(1 for d in devices if d.get("type") == "gateway")
    other_devices = total - switches - aps - gateways

    wireless_clients = sum(1 for c in clients if c.get("type") == "wireless")
    wired_clients = sum(1 for c in clients if c.get("type") == "wired")

    stats = {
        # Existing keys — semantics unchanged.
        "total_devices": total,
        "online_devices": online,
        "offline_devices": total - online,
        "online_pct": int(online / total * 100) if total else 0,
        "total_clients": len(clients),
        "switches": switches,
        "aps": aps,
        "gateways": gateways,
        "sites": sites_count,
        # New (additive) keys.
        "unknown_devices": unknown,
        "other_devices": other_devices,
        "wireless_clients": wireless_clients,
        "wired_clients": wired_clients,
        "switch_pct": _pct(switches, total),
        "ap_pct": _pct(aps, total),
        "gateway_pct": _pct(gateways, total),
        "wireless_pct": _pct(wireless_clients, len(clients)),
        "wired_pct": _pct(wired_clients, len(clients)),
    }

    donut_segments = _donut_segments(
        [(online, "#4ade80"), (offline_strict, "#f87171"), (unknown, "#64748b")],
        total,
    )

    device_mix = [
        {"label": "Switches", "count": switches, "color": "#818cf8",
         "pct": f"{(switches / total * 100) if total else 0:.1f}", "share": _pct(switches, total)},
        {"label": "Access Points", "count": aps, "color": "#c084fc",
         "pct": f"{(aps / total * 100) if total else 0:.1f}", "share": _pct(aps, total)},
        {"label": "Gateways", "count": gateways, "color": "#fbbf24",
         "pct": f"{(gateways / total * 100) if total else 0:.1f}", "share": _pct(gateways, total)},
    ]
    if other_devices > 0:
        device_mix.append(
            {"label": "Other", "count": other_devices, "color": "#64748b",
             "pct": f"{other_devices / total * 100:.1f}", "share": _pct(other_devices, total)}
        )

    client_total = len(clients)
    client_mix = [
        {"label": "Wireless", "count": wireless_clients, "color": "#60a5fa",
         "pct": f"{(wireless_clients / client_total * 100) if client_total else 0:.1f}"},
        {"label": "Wired", "count": wired_clients, "color": "#2dd4bf",
         "pct": f"{(wired_clients / client_total * 100) if client_total else 0:.1f}"},
    ]

    events = await _recent_events(devices)

    updated = datetime.now(timezone.utc).strftime("%I:%M %p UTC")

    context = {
        "stats": stats,
        "devices": devices[:10],
        "updated": updated,
        "active": "home",
        "donut_segments": donut_segments,
        "device_mix": device_mix,
        "client_mix": client_mix,
        "events": events,
        "is_partial": bool(partial),
    }

    if partial:
        return _render_live_fragment(request, context)

    return templates.TemplateResponse(request, "home.html", context)
