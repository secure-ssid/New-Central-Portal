"""Login / logout routes for the session-auth layer.

The enforcement itself lives in the middleware in main.py; these routes just
mint and clear the signed session cookie.
"""
import asyncio
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import security
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Constant delay on failed logins to slow down brute forcing.
FAILED_LOGIN_DELAY_SECONDS = 0.5


def _render_login(request: Request, next_path: str, error: str | None = None,
                  status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_path, "error": error},
        status_code=status_code,
    )


@router.get("/login")
async def login_page(request: Request):
    """Standalone login page. Redirects home when auth is disabled or the
    visitor already holds a valid session."""
    if not security.auth_enabled():
        return RedirectResponse("/", status_code=303)
    next_path = security.sanitize_next(request.query_params.get("next", "/"))
    if security.verify_session_token(request.cookies.get(security.SESSION_COOKIE)):
        return RedirectResponse(next_path, status_code=303)
    return _render_login(request, next_path)


@router.post("/login")
async def login_submit(request: Request, password: str = Form(""),
                       next: str = Form("/")):
    if not security.auth_enabled():
        return RedirectResponse("/", status_code=303)
    ip = security.client_ip(request)
    next_path = security.sanitize_next(next)

    if security.login_limiter.is_limited(ip):
        logger.warning("Login rate limit exceeded for %s", ip)
        return _render_login(
            request, next_path,
            error="Too many failed attempts — try again in a few minutes.",
            status_code=429,
        )

    if not security.verify_password(password):
        security.login_limiter.record_failure(ip)
        logger.warning("Failed login attempt from %s", ip)
        await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
        return _render_login(request, next_path, error="Invalid password",
                             status_code=401)

    security.login_limiter.reset(ip)
    logger.info("Successful login from %s", ip)
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        key=security.SESSION_COOKIE,
        value=security.create_session_token(),
        max_age=security.session_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=security.is_secure_request(request),
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    logger.info("Logout from %s", security.client_ip(request))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(key=security.SESSION_COOKIE, path="/")
    return response


@router.get("/auth/whoami")
async def whoami(request: Request):
    """Lightweight auth probe (exempt from the middleware) so client code or
    docs can detect login state without triggering a redirect."""
    if not security.auth_enabled():
        return JSONResponse({"authenticated": True, "auth_disabled": True})
    ok = security.verify_session_token(request.cookies.get(security.SESSION_COOKIE))
    return JSONResponse({"authenticated": bool(ok), "auth_disabled": False})
