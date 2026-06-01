from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(alias="DATABASE_URL")

    # LLM providers — all optional; the active one is chosen via EDM_LLM_PROVIDER.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    # Embedding provider — chosen via EDM_EMBEDDING_PROVIDER.
    voyage_api_key: str | None = Field(default=None, alias="VOYAGE_API_KEY")
    voyage_model: str = Field(default="voyage-3", alias="VOYAGE_MODEL")

    github_app_id: str | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_private_key_path: Path | None = Field(default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH")
    github_webhook_secret: str | None = Field(default=None, alias="GITHUB_WEBHOOK_SECRET")

    target_repo: str | None = Field(default=None, alias="TARGET_REPO")

    host: str = Field(default="127.0.0.1", alias="EDM_HOST")
    port: int = Field(default=8088, alias="EDM_PORT")
    log_level: str = Field(default="INFO", alias="EDM_LOG_LEVEL")

    # Auth
    session_secret: str = Field(default="dev-only-change-me", alias="EDM_SESSION_SECRET")
    admin_user: str = Field(default="admin", alias="EDM_ADMIN_USER")
    admin_password: str = Field(default="admin", alias="EDM_ADMIN_PASSWORD")
    session_max_age_hours: int = Field(default=24, alias="EDM_SESSION_MAX_AGE_HOURS")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
