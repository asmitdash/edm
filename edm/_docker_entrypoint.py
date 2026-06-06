"""Docker entrypoint: ensure pgvector + pgcrypto exist (idempotent), apply
all migrations under edm/_migrations/ in order, then exec uvicorn.

The image runs as a non-root user, so it cannot CREATE EXTENSION. Our compose
seeds the extensions via Postgres init scripts; this entrypoint just applies
the table migrations idempotently."""

from __future__ import annotations

import os
import sys
from importlib import resources
from pathlib import Path
from time import sleep

import psycopg


def _wait_for_db(url: str, *, attempts: int = 30, delay: float = 1.0) -> None:
    last_err: Exception | None = None
    psy_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    for i in range(attempts):
        try:
            with psycopg.connect(psy_url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
                return
        except Exception as e:
            last_err = e
            sleep(delay)
    raise RuntimeError(f"Postgres not reachable after {attempts}s: {last_err}")


def _apply_migrations(url: str) -> None:
    psy_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    migrations_dir = resources.files("edm").joinpath("_migrations")
    files = sorted(p for p in migrations_dir.iterdir() if p.name.endswith(".sql"))
    with psycopg.connect(psy_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  name TEXT PRIMARY KEY,"
                "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
                ")"
            )
            cur.execute("SELECT name FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
            if not applied:
                # Pre-ledger DB: if core tables already exist, treat all
                # migrations as already applied (backfill the ledger).
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'sources'"
                )
                if cur.fetchone():
                    print(
                        f"[entrypoint] pre-existing schema detected; "
                        f"backfilling ledger with {len(files)} migrations",
                        flush=True,
                    )
                    for f in files:
                        cur.execute(
                            "INSERT INTO schema_migrations (name) VALUES (%s)",
                            (f.name,),
                        )
                    applied = {f.name for f in files}
        for f in files:
            if f.name in applied:
                print(f"[entrypoint] skip {f.name} (already applied)", flush=True)
                continue
            sql = f.read_text(encoding="utf-8")
            print(f"[entrypoint] applying {f.name}", flush=True)
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (f.name,),
                )


def main() -> None:
    url = os.environ.get("DATABASE_URL", "postgresql+psycopg://edm:edm@postgres:5432/edm")
    _wait_for_db(url)
    _apply_migrations(url)
    print("[entrypoint] migrations applied; starting uvicorn", flush=True)
    os.execvp("uvicorn", ["uvicorn", "edm.api.server:app", "--host", "0.0.0.0",
                          "--port", os.environ.get("EDM_PORT", "8088")])


if __name__ == "__main__":
    main()
