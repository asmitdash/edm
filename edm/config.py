from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from edm import runtime_config


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Default to empty string — the auto-setup wizard fills this in via the
    # runtime config file (~/.edm/runtime.json). Code that needs a working DB
    # connection should call `require_database()` rather than reading directly.
    database_url: str = Field(default="", alias="DATABASE_URL")

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
    # Push runtime-config values into the environment first so pydantic-settings
    # picks them up. This is what makes the auto-setup wizard work without the
    # user touching .env.
    runtime_config.load_into_env()
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """After the wizard saves the database URL we need to rebuild Settings so
    subsequent get_settings() calls see the new value."""
    get_settings.cache_clear()


def require_database() -> str:
    """Resolve the DB URL or raise a friendly error directing the user to /setup/database."""
    s = get_settings()
    if not s.database_url:
        raise RuntimeError(
            "DATABASE_URL is not configured. Visit /setup/database in the UI "
            "or run `edm setup` from the CLI."
        )
    return s.database_url
