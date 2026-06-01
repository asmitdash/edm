"""Test helpers: insert seed sources and run the pipeline against them."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope


def insert_source(*, kind: str, external_id: str, title: str, body: str, days_ago: int = 0,
                  author: str = "tester", repo: str = "demo/repo") -> UUID:
    occurred_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with session_scope() as session:
        sid = session.execute(
            text(
                """
                INSERT INTO sources
                  (kind, external_id, repo, url, title, body, author,
                   occurred_at, content_hash, metadata)
                VALUES
                  (:kind, :ext, :repo, :url, :title, :body, :author,
                   :occurred_at, :hash, CAST(:meta AS JSONB))
                RETURNING id
                """
            ),
            {
                "kind": kind,
                "ext": external_id,
                "repo": repo,
                "url": f"https://example.com/{external_id}",
                "title": title,
                "body": body,
                "author": author,
                "occurred_at": occurred_at,
                "hash": hashlib.sha256(body.encode()).hexdigest(),
                "meta": json.dumps({"test": True}),
            },
        ).scalar_one()
    return sid


SEED_POSTGRES = {
    "kind": "pr",
    "external_id": "demo/repo#101",
    "title": "Adopt Postgres for primary OLTP store",
    "body": (
        "We will use Postgres 16 as the primary OLTP store across all services. "
        "Reasoning: strong relational guarantees, mature tooling, our team's existing expertise, "
        "and pgvector support for embedding workloads. Constraint: the read path must sustain "
        "P99 < 100ms at 5x current peak. Assumption: write throughput will not exceed 5k tps in "
        "the next 18 months. Considered alternatives: MongoDB (rejected: weaker transactional "
        "semantics for financial workflows) and DynamoDB (rejected: vendor lock-in)."
    ),
    "days_ago": 120,
}

SEED_MONGO = {
    "kind": "pr",
    "external_id": "demo/repo#214",
    "title": "Move user-events service to MongoDB",
    "body": (
        "Migrating the user-events service from Postgres to MongoDB. "
        "Justification: schemaless flexibility for evolving event payloads, and we expect "
        "write throughput to spike to ~12k tps during product launches. The events table in "
        "Postgres has been a hotspot."
    ),
    "days_ago": 3,
}
