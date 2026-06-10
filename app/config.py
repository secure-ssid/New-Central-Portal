"""Application settings loaded from environment."""
import logging
import os

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    aruba_central_base_url: str = ""
    aruba_central_access_token: str = ""
    anthropic_api_key: str = ""
    github_token: str = ""
    database_url: str = "postgresql://netlab:netlab@db:5432/netlab"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

# Placeholder values that should be treated the same as "not configured".
_PLACEHOLDERS = {"your_key_here", "your_github_pat_here", "changeme", ""}


def validate_settings() -> list[str]:
    """Check required/important settings and log clear warnings.

    Never raises — the app should start in a degraded mode (mock data,
    disabled integrations) rather than crash. Returns the list of warning
    messages for callers that want to surface them.
    """
    warnings: list[str] = []

    classic_missing = [
        v for v in (
            "CLASSIC_CENTRAL_BASE_URL",
            "CLASSIC_CENTRAL_CLIENT_ID",
            "CLASSIC_CENTRAL_CLIENT_SECRET",
        )
        if not os.environ.get(v)
    ]
    if classic_missing:
        warnings.append(
            "Classic Central credentials missing (%s) — group/site management "
            "features will be unavailable." % ", ".join(classic_missing)
        )

    if settings.aruba_central_base_url.strip() in _PLACEHOLDERS or \
            settings.aruba_central_access_token.strip() in _PLACEHOLDERS:
        warnings.append(
            "Aruba Central base URL/token not configured — device and client "
            "views may fall back to mock data."
        )

    if not os.environ.get("DATABASE_URL"):
        warnings.append(
            "DATABASE_URL not set — using built-in development default. "
            "Set DATABASE_URL explicitly for non-dev deployments."
        )

    for msg in warnings:
        logger.warning("Config: %s", msg)
    return warnings
