"""Shared Jinja2 environment with portal-wide template globals."""
import hashlib
import logging
import os

from fastapi.templating import Jinja2Templates

import security

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
templates.env.globals["auth_enabled"] = security.auth_enabled
templates.env.globals["use_built_tailwind"] = os.environ.get(
    "USE_BUILT_TAILWIND", ""
).lower() in ("1", "true", "yes")


def asset_url(path: str) -> str:
    """Return a static path with a content-hash query string.

    The generated stylesheets (dist/tailwind.css, app.css) are not
    version-pinned in their filenames, so a browser that cached them — under
    the heuristic freshness it applied back when these responses carried no
    Cache-Control at all — could keep serving the old CSS against new markup
    long after a rebuild. Hashing the bytes into the URL makes a rebuild a new
    URL, so the fix reaches the user on the next page load.
    """
    rel = path.lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    try:
        with open(os.path.join("static", rel), "rb") as fh:
            digest = hashlib.blake2b(fh.read(), digest_size=8).hexdigest()
    except OSError:
        logger.debug("asset_url: %s not readable; serving unversioned", path)
        return path
    return f"{path}?v={digest}"


templates.env.globals["asset_url"] = asset_url
