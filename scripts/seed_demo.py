"""Seed two synthetic, contradictory PRs so you can demo the pipeline without
needing a real GitHub repo. Useful before plugging in a real TARGET_REPO.

Usage:
  python scripts/seed_demo.py
  edm process
  edm findings
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from edm.db import session_scope


SEEDS = [
    {
        "kind": "pr",
        "external_id": "demo/repo#101",
        "repo": "demo/repo",
        "url": "https://example.com/demo/repo/pull/101",
        "title": "Adopt Postgres for primary OLTP store",
        "body": (
            "We will use Postgres 16 as the primary OLTP store across all services. "
            "Reasoning: strong relational guarantees, mature tooling, our team's existing expertise, "
            "and pgvector support for embedding workloads. Constraint: the read path must sustain "
            "P99 < 100ms at 5x current peak. Assumption: write throughput will not exceed 5k tps in "
            "the next 18 months. Considered alternatives: MongoDB (rejected: weaker transactional "
            "semantics for financial workflows) and DynamoDB (rejected: vendor lock-in plus we need "
            "complex JOINs for reporting)."
        ),
        "author": "alice",
        "days_ago": 120,
    },
    {
        "kind": "pr",
        "external_id": "demo/repo#214",
        "repo": "demo/repo",
        "url": "https://example.com/demo/repo/pull/214",
        "title": "Move user-events service to MongoDB",
        "body": (
            "Migrating the user-events service from Postgres to MongoDB. "
            "Justification: schemaless flexibility for evolving event payloads, and we expect "
            "write throughput to spike to ~12k tps during product launches. The events table in "
            "Postgres has been a hotspot. We accept the loss of strict transactional guarantees "
            "for this service since events are append-only and idempotent."
        ),
        "author": "bob",
        "days_ago": 3,
    },
]


def main() -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for s in SEEDS:
            occurred_at = now - timedelta(days=s["days_ago"])
            session.execute(
                text(
                    """
                    INSERT INTO sources
                      (kind, external_id, repo, url, title, body, author,
                       occurred_at, content_hash, metadata)
                    VALUES
                      (:kind, :ext, :repo, :url, :title, :body, :author,
                       :occurred_at, :hash, CAST(:meta AS JSONB))
                    ON CONFLICT (kind, external_id) DO NOTHING
                    """
                ),
                {
                    "kind": s["kind"],
                    "ext": s["external_id"],
                    "repo": s["repo"],
                    "url": s["url"],
                    "title": s["title"],
                    "body": s["body"],
                    "author": s["author"],
                    "occurred_at": occurred_at,
                    "hash": hashlib.sha256(s["body"].encode()).hexdigest(),
                    "meta": json.dumps({"seeded": True}),
                },
            )
    print(f"seeded {len(SEEDS)} demo sources")


if __name__ == "__main__":
    main()
