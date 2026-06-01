"""Pytest config: spin up a fresh test DB on a temp schema, force the stub
providers, and yield a session-scoped fixture that resets per test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text


# Force stub providers BEFORE importing edm.* — config + provider singletons
# read env at first access.
os.environ.setdefault("EDM_LLM_PROVIDER", "stub")
os.environ.setdefault("EDM_EMBEDDING_PROVIDER", "stub")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://edm:edm@127.0.0.1:5432/edm_test")


def _ensure_test_db() -> None:
    """Create the edm_test database if it doesn't exist, using the admin
    connection string set in EDM_TEST_ADMIN_DATABASE_URL (defaults to the
    main edm DB on the same cluster)."""
    import psycopg

    admin_url = os.environ.get(
        "EDM_TEST_ADMIN_DATABASE_URL",
        "postgresql://edm:edm@127.0.0.1:5432/postgres",
    )
    with psycopg.connect(admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'edm_test'")
            if cur.fetchone() is None:
                cur.execute("CREATE DATABASE edm_test OWNER edm")


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_schema():
    _ensure_test_db()

    from edm.db import get_engine

    engine = get_engine()
    migration = Path(__file__).resolve().parents[1] / "migrations" / "001_init.sql"
    sql = migration.read_text(encoding="utf-8")

    # Drop our application tables individually instead of `DROP SCHEMA public
    # CASCADE` — the latter would also nuke the `vector` and `pgcrypto`
    # extensions that the edm role isn't privileged to recreate.
    drop_sql = """
    DROP TABLE IF EXISTS
        conflict_findings, stale_assumption_findings, edges,
        decision_alternatives, decision_owners, decisions, assumptions,
        constraints, source_chunks, extractions, sources, owners
    CASCADE;
    """
    with engine.begin() as conn:
        conn.execute(text(drop_sql))
    with engine.begin() as conn:
        conn.execute(text(sql))
    yield


@pytest.fixture(autouse=True)
def _reset_data():
    """Truncate all rows between tests — keeps schema, drops content."""
    from edm.db import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                  conflict_findings,
                  stale_assumption_findings,
                  edges,
                  decision_alternatives,
                  decision_owners,
                  decisions,
                  assumptions,
                  constraints,
                  source_chunks,
                  extractions,
                  sources,
                  owners
                RESTART IDENTITY CASCADE
                """
            )
        )
    yield
