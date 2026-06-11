"""Custom error pages (404 / 500) for the portal.

Wiring (applied by the coordinator in ``main.py`` — see ROUND5_WIRING.md):

    from errors import register_error_handlers
    register_error_handlers(app)

Place the two lines after ``app = FastAPI(...)`` is created. Nothing else
is required.

STATIC_MOUNT_NEEDED = False
    ``main.py`` already mounts static files
    (``app.mount("/static", StaticFiles(directory="static"), name="static")``),
    and the error templates are fully self-contained (inline CSS, no static
    assets, they do not extend base.html), so they render correctly even if
    the static mount were absent.

Behaviour:
    * 404 (any ``StarletteHTTPException`` with status 404) renders
      ``templates/errors/404.html``.
    * Explicit 5xx ``HTTPException``s and any unhandled exception render
      ``templates/errors/500.html``; unhandled exceptions are logged with the
      full traceback. The page never echoes internal exception text.
    * Requests that look like API/HTMX calls (``HX-Request: true`` header or
      ``Accept: application/json``) get ``{"detail": ...}`` JSON instead of
      HTML, with the same status code.
    * All other HTTP status codes fall through to FastAPI's default handler,
      so JSON API error shapes (400/403/422...) are unchanged.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler as _default_http_handler
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# main.py already mounts /static — see module docstring.
STATIC_MOUNT_NEEDED = False

# Local Jinja environment anchored to this file so the handlers work
# regardless of the process working directory.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _wants_json(request: Request) -> bool:
    """HTMX partial swaps and API clients should get JSON, not a full page."""
    if request.headers.get("hx-request", "").lower() == "true":
        return True
    return "application/json" in request.headers.get("accept", "").lower()


def _error_response(request: Request, status_code: int, detail: str,
                    template_name: str, page_message: str | None = None):
    if _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=status_code)
    return _templates.TemplateResponse(
        request,
        template_name,
        {"detail": page_message if page_message is not None else detail},
        status_code=status_code,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach the custom 404/500 handlers to ``app``."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = str(exc.detail) if exc.detail else ""
        if exc.status_code == 404:
            return _error_response(
                request, 404, detail or "Not Found", "errors/404.html",
                page_message=detail,
            )
        if exc.status_code >= 500:
            logger.error(
                "HTTP %s on %s %s: %s",
                exc.status_code, request.method, request.url.path, detail,
            )
            return _error_response(
                request, exc.status_code, detail or "Internal Server Error",
                "errors/500.html", page_message="",
            )
        # Anything else (400, 403, 422...): keep FastAPI's default behaviour.
        return await _default_http_handler(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception on %s %s",
            request.method, request.url.path, exc_info=exc,
        )
        # Never leak internals: generic JSON detail, friendly page message.
        return _error_response(
            request, 500, "Internal Server Error", "errors/500.html",
            page_message="",
        )
