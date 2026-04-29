"""Application settings loaded from environment."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aruba_central_base_url: str = ""
    aruba_central_access_token: str = ""
    anthropic_api_key: str = ""
    github_token: str = ""
    database_url: str = "postgresql://netlab:netlab@db:5432/netlab"

    class Config:
        env_file = ".env"


settings = Settings()
