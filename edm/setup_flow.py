"""Shared setup-flow helpers used by both the CLI (`edm setup`) and the
web wizard (`/setup`)."""

from __future__ import annotations

from typing import Any

from edm import audit, install_config, runtime_config, users
from edm.extract.providers import build_provider


def is_database_configured() -> bool:
    """Truthy DATABASE_URL via runtime config or env, AND we can reach the DB."""
    if not runtime_config.is_database_configured():
        return False
    try:
        from sqlalchemy import text
        from edm.db import get_engine
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def is_setup_complete() -> bool:
    """The install is 'set up' once: DB + admin + LLM are all configured.
    GitHub / Gmail remain optional."""
    if not is_database_configured():
        return False
    try:
        if not users.has_active_admin():
            return False
        if not install_config.get_llm_config().provider:
            return False
    except Exception:
        return False
    return True


def ensure_no_admin() -> None:
    if users.has_any_user():
        raise RuntimeError("setup has already been completed; run /settings to make changes")


def create_admin(username: str, password: str) -> users.User:
    ensure_no_admin()
    user = users.create_user(username, password, "admin")
    audit.log(user_id=user.id, action="admin.create", target=user.username)
    return user


def validate_and_save_llm(*, provider: str, model: str, api_key: str, configured_by) -> dict[str, Any]:
    """Make a tiny extraction call to confirm the key works, then save."""
    p = build_provider(provider, api_key=api_key, model=model)
    test_schema = {
        "name": "ping",
        "input_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    }
    try:
        result = p.extract(
            system="You confirm setup. Reply with the schema, ok=true.",
            user="Confirm provider works. Reply ok=true.",
            schema=test_schema,
        )
    except Exception as e:  # provider failure or key invalid
        return {"ok": False, "error": str(e)}
    install_config.save_llm_config(
        provider=provider, model=model, api_key=api_key,
        configured_by=configured_by, status="ok",
    )
    audit.log(user_id=configured_by, action="llm.configure", target=provider, metadata={"model": model})
    return {"ok": True, "tokens_in": result.input_tokens, "tokens_out": result.output_tokens}
