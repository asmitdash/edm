"""Auto-setup orchestration.

What runs once the user has entered a working database URL:
  1. validate_database(url) — connectivity + version + extensions check
  2. apply_migrations() — idempotent; tracks applied files in `schema_migrations`
  3. ensure_seed_sops() — loads bundled seed library (once)
  4. ensure_session_secret() — auto-generates if absent
  5. mark setup_complete in runtime config

The wizard calls these in order. Each step is safe to re-run.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from edm import runtime_config
from edm.logging import get_logger

log = get_logger(__name__)


# ---------- DB validation ----------


_DSN_RE = re.compile(r"^postgresql(\+psycopg)?://", re.IGNORECASE)


def normalize_database_url(url: str) -> str:
    """Accept both `postgresql://` and `postgresql+psycopg://`. Coerce to the
    `+psycopg` form SQLAlchemy + pydantic-settings expect."""
    url = url.strip()
    if not url:
        raise ValueError("Empty database URL.")
    if not _DSN_RE.match(url):
        raise ValueError("Database URL must start with postgresql:// or postgresql+psycopg://")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def validate_database(url: str) -> dict[str, Any]:
    """Open a connection, check version + required extensions.

    Returns a dict with `ok`, `version`, `has_pgvector`, `has_pgcrypto`, and
    a human-readable `message`. Does NOT mutate runtime config; the caller
    decides whether to persist after validation.
    """
    out: dict[str, Any] = {
        "ok": False, "version": None,
        "has_pgvector": False, "has_pgcrypto": False,
        "message": "",
    }
    try:
        engine = create_engine(url, pool_pre_ping=True, future=True)
        with engine.connect() as c:
            ver = c.execute(text("SHOW server_version")).scalar_one()
            out["version"] = ver
            exts = c.execute(text("SELECT extname FROM pg_extension")).scalars().all()
            out["has_pgvector"] = "vector" in exts
            out["has_pgcrypto"] = "pgcrypto" in exts
        engine.dispose()
    except Exception as e:
        out["message"] = f"Could not connect: {e}"
        return out

    missing = []
    if not out["has_pgcrypto"]:
        missing.append("pgcrypto")
    if not out["has_pgvector"]:
        missing.append("vector")
    if missing:
        out["message"] = (
            f"Connected, but missing required extension(s): {', '.join(missing)}. "
            "Run as a Postgres superuser: "
            "CREATE EXTENSION IF NOT EXISTS pgcrypto; CREATE EXTENSION IF NOT EXISTS vector;"
        )
        return out

    out["ok"] = True
    out["message"] = f"Connected to Postgres {ver} with pgvector and pgcrypto."
    return out


# ---------- Migrations ----------


def _migrations_iter() -> list[tuple[str, str]]:
    """Yield (name, sql) for every migration in NNN order.

    Looks first at the in-tree `migrations/` dir (development), then at the
    packaged copy at `edm._migrations` (installed wheel).
    """
    found: list[tuple[str, str]] = []

    repo_dir = Path(__file__).resolve().parent.parent / "migrations"
    if repo_dir.is_dir():
        for p in sorted(repo_dir.glob("*.sql")):
            if p.name[:3].isdigit():
                found.append((p.name, p.read_text(encoding="utf-8")))
        if found:
            return found

    try:
        pkg = resources.files("edm._migrations")
        for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
            if entry.is_file() and entry.name.endswith(".sql") and entry.name[:3].isdigit():
                found.append((entry.name, entry.read_text(encoding="utf-8")))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    return found


_TRACKER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def apply_migrations(*, force_url: str | None = None) -> dict[str, Any]:
    """Apply every migration NOT yet recorded in `schema_migrations`. Idempotent.

    Returns {"applied": [...], "skipped": [...], "errors": [...]}.
    """
    from edm.db import get_engine, reset_engine
    from edm.config import reset_settings_cache

    if force_url:
        runtime_config.update(database_url=force_url)
        reset_settings_cache()
        reset_engine()

    engine = get_engine()
    out: dict[str, Any] = {"applied": [], "skipped": [], "errors": []}

    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(_TRACKER_DDL)
        cur.execute("SELECT name FROM schema_migrations")
        applied = {r[0] for r in cur.fetchall()}
        raw.commit()

        for name, sql in _migrations_iter():
            if name in applied:
                out["skipped"].append(name)
                continue
            try:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
                raw.commit()
                out["applied"].append(name)
                log.info("migration.applied", name=name)
            except Exception as e:
                raw.rollback()
                out["errors"].append({"name": name, "error": str(e)})
                log.error("migration.failed", name=name, error=str(e))
                break  # don't keep going past the first failure
    finally:
        raw.close()

    return out


# ---------- Seed SOPs ----------


def ensure_seed_sops() -> int:
    """Load the bundled seed SOP library into the corpus once."""
    if runtime_config.get("auto_seed_done"):
        return 0
    seed_dir = Path(__file__).resolve().parent / "sop" / "seed_library"
    if not seed_dir.is_dir():
        return 0
    from edm.sop.corpus import load_seed_library_from_disk
    n = load_seed_library_from_disk(seed_dir)
    runtime_config.update(auto_seed_done=True, seed_count=n)
    log.info("seed.loaded", count=n)
    return n


# ---------- Top-level orchestration ----------


def configure_database(url: str) -> dict[str, Any]:
    """Wizard step: validate the URL, persist it, run migrations, seed SOPs.

    Returns a structured result the wizard renders as success / per-step failure.
    """
    result: dict[str, Any] = {
        "ok": False, "validation": None, "migrations": None,
        "seed_count": 0, "session_secret_generated": False,
        "message": "",
    }
    try:
        url = normalize_database_url(url)
    except ValueError as e:
        result["message"] = str(e)
        return result

    result["validation"] = validate_database(url)
    if not result["validation"]["ok"]:
        result["message"] = result["validation"]["message"]
        return result

    runtime_config.update(database_url=url)

    secret_before = runtime_config.get("session_secret")
    runtime_config.ensure_session_secret()
    result["session_secret_generated"] = secret_before is None

    from edm.config import reset_settings_cache
    from edm.db import reset_engine
    reset_settings_cache()
    reset_engine()

    mig = apply_migrations()
    result["migrations"] = mig
    if mig["errors"]:
        result["message"] = f"Migrations failed: {mig['errors']}"
        return result

    try:
        result["seed_count"] = ensure_seed_sops()
    except Exception as e:
        log.warning("seed.failed", error=str(e))

    result["ok"] = True
    result["message"] = (
        f"Connected, applied {len(mig['applied'])} migration(s), "
        f"seeded {result['seed_count']} SOP template(s)."
    )
    return result
