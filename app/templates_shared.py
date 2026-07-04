"""Shared Jinja2 environment with portal-wide template globals."""
import os

from fastapi.templating import Jinja2Templates

import security

templates = Jinja2Templates(directory="templates")
templates.env.globals["auth_enabled"] = security.auth_enabled
templates.env.globals["use_built_tailwind"] = os.environ.get(
    "USE_BUILT_TAILWIND", ""
).lower() in ("1", "true", "yes")
