"""Shared Jinja2 environment with portal-wide template globals."""
import hashlib
import logging
import os

from fastapi.templating import Jinja2Templates

import security

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
templates.env.globals["auth_enabled"] = security.auth_enabled

# The built stylesheet is not tracked in git (only in a machine-local
# .git/info/exclude) and the ./app bind mount shadows the copy baked into the
# image, so a fresh clone or a plain rebuild can ship with dist/tailwind.css
# absent. Requiring the file to actually exist means that case falls back to
# the Play-CDN branch and renders styled, instead of emitting a link to a 404
# and rendering the whole portal unstyled with no other signal.
_want_built = os.environ.get("USE_BUILT_TAILWIND", "").lower() in ("1", "true", "yes")
_built_present = os.path.exists(os.path.join("static", "dist", "tailwind.css"))
if _want_built and not _built_present:
    logger.warning(
        "USE_BUILT_TAILWIND is set but static/dist/tailwind.css is missing; "
        "falling back to the Tailwind Play CDN so the portal still renders styled."
    )
templates.env.globals["use_built_tailwind"] = _want_built and _built_present


# path -> (st_mtime_ns, st_size, digest). Avoids re-reading and re-hashing
# ~59 KB of CSS on every single page render — a stat() is cheap, the hash is
# recomputed only when the file's mtime or size changes (i.e. after a rebuild),
# which keeps the cache-busting correct.
_asset_hash_cache: dict[str, tuple[int, int, str]] = {}


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
    full = os.path.join("static", rel)
    try:
        st = os.stat(full)
    except OSError:
        logger.debug("asset_url: %s not readable; serving unversioned", path)
        return path
    cached = _asset_hash_cache.get(path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return f"{path}?v={cached[2]}"
    try:
        with open(full, "rb") as fh:
            digest = hashlib.blake2b(fh.read(), digest_size=8).hexdigest()
    except OSError:
        logger.debug("asset_url: %s not readable; serving unversioned", path)
        return path
    _asset_hash_cache[path] = (st.st_mtime_ns, st.st_size, digest)
    return f"{path}?v={digest}"


templates.env.globals["asset_url"] = asset_url
