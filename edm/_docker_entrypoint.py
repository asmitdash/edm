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
        for f in files:
            sql = f.read_text(encoding="utf-8")
            print(f"[entrypoint] applying {f.name}", flush=True)
            with conn.cursor() as cur:
                cur.execute(sql)


def main() -> None:
    url = os.environ.get("DATABASE_URL", "postgresql+psycopg://edm:edm@postgres:5432/edm")
    _wait_for_db(url)
    _apply_migrations(url)
    print("[entrypoint] migrations applied; starting uvicorn", flush=True)
    os.execvp("uvicorn", ["uvicorn", "edm.api.server:app", "--host", "0.0.0.0",
                          "--port", os.environ.get("EDM_PORT", "8088")])


if __name__ == "__main__":
    main()
