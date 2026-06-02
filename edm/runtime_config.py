"""Runtime config persisted across restarts.

Stored at ~/.edm/runtime.json (override with EDM_RUNTIME_DIR). Holds the values
the user enters in the setup wizard that need to outlive process restarts:

  - database_url
  - session_secret (auto-generated once; rotation invalidates sessions and
    Fernet-encrypted keys, so we treat this as install-stable)
  - voyage_api_key + embedding_provider
  - gmail token / scope (optional)
  - flags: setup_complete, auto_seed_done, ...

Why a JSON file rather than env vars:
  * env vars don't survive a `docker compose down`
  * we cannot persist DATABASE_URL inside the DB itself (chicken-and-egg)
  * single-tenant single-host deployment — one file is fine

The file is chmod 600 on POSIX. On Windows we just rely on user-profile ACLs.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any


def runtime_dir() -> Path:
    override = os.environ.get("EDM_RUNTIME_DIR")
    if override:
        p = Path(override)
    else:
        p = Path.home() / ".edm"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runtime_path() -> Path:
    return runtime_dir() / "runtime.json"


def load() -> dict[str, Any]:
    p = runtime_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(data: dict[str, Any]) -> None:
    p = runtime_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def update(**kwargs: Any) -> dict[str, Any]:
    data = load()
    data.update({k: v for k, v in kwargs.items() if v is not None})
    save(data)
    return data


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def ensure_session_secret() -> str:
    """Return the install-stable session secret, generating once if missing."""
    data = load()
    if not data.get("session_secret"):
        data["session_secret"] = secrets.token_urlsafe(48)
        save(data)
    return data["session_secret"]


def load_into_env() -> None:
    """Push runtime values into os.environ BEFORE pydantic-settings reads them.

    Only sets a key when the env var is not already present, so explicit env
    vars (Docker compose, CI) still win.
    """
    data = load()

    pushes = {
        "DATABASE_URL": data.get("database_url"),
        "EDM_SESSION_SECRET": data.get("session_secret"),
        "VOYAGE_API_KEY": data.get("voyage_api_key"),
        "EDM_EMBEDDING_PROVIDER": data.get("embedding_provider"),
    }
    for k, v in pushes.items():
        if v and not os.environ.get(k):
            os.environ[k] = str(v)


def is_database_configured() -> bool:
    return bool(load().get("database_url") or os.environ.get("DATABASE_URL"))
