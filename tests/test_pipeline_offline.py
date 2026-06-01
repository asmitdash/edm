"""End-to-end pipeline test using stub LLM + stub embeddings.

Verifies the full wiring without spending API tokens:
  source rows -> extraction (stub) -> graph nodes/edges -> contradiction detection.

If this passes, the pipeline shape is correct. The only thing it does NOT
verify is the LLM's actual reasoning quality — that's reserved for the live
Gemini run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from edm.db import session_scope
from edm.pipeline import process_source


POSTGRES_PR = {
    "kind": "pr",
    "external_id": "demo/repo#101",
    "repo": "demo/repo",
    "url": "https://example.com/demo/repo/pull/101",
    "title": "Adopt Postgres for primary OLTP store",
    "body": (
        "We will use Postgres 16 as the primary OLTP store across all services. "
        "Reasoning: strong relational guarantees, mature tooling, our team's "
        "existing expertise, and pgvector support. Constraint: read path must "
        "sustain P99 < 100ms at projected peak. Assumption: write throughput "
        "will not exceed 5k tps in the next 18 months. Considered alternatives: "
        "MongoDB (rejected: weaker transactional semantics for financial "
        "workflows)."
    ),
    "author": "alice",
    "days_ago": 120,
}

MONGO_PR = {
    "kind": "pr",
    "external_id": "demo/repo#214",
    "repo": "demo/repo",
    "url": "https://example.com/demo/repo/pull/214",
    "title": "Move user-events service to MongoDB",
    "body": (
        "Migrating the user-events service from Postgres to MongoDB. Justification: "
        "we expect write throughput to spike to ~12k tps during product launches. "
        "The events table in Postgres has been a hotspot."
    ),
    "author": "bob",
    "days_ago": 3,
}


def _seed(s, source: dict):
    occurred_at = datetime.now(timezone.utc) - timedelta(days=source["days_ago"])
    row = s.execute(
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
            "kind": source["kind"],
            "ext": source["external_id"],
            "repo": source["repo"],
            "url": source["url"],
            "title": source["title"],
            "body": source["body"],
            "author": source["author"],
            "occurred_at": occurred_at,
            "hash": hashlib.sha256(source["body"].encode()).hexdigest(),
            "meta": json.dumps({"seeded": True}),
        },
    ).mappings().first()
    return row["id"]


def test_extracts_decision_assumption_constraint_from_postgres_pr():
    with session_scope() as s:
        sid = _seed(s, POSTGRES_PR)

    summary = process_source(sid)

    assert summary["decisions"] == 1
    assert summary["assumptions"] == 1
    assert summary["constraints"] == 1
    # No prior decisions exist yet, so contradiction list is empty.
    assert summary["findings"] == []

    with session_scope() as s:
        rows = s.execute(text("SELECT title FROM decisions")).all()
        assert any("Postgres" in r[0] for r in rows)
        # Edges connect the decision to its assumption and constraint.
        edge_count = s.execute(text("SELECT COUNT(*) FROM edges")).scalar_one()
        assert edge_count >= 2


def test_contradiction_detected_when_mongo_pr_lands_after_postgres_decision():
    with session_scope() as s:
        sid_pg = _seed(s, POSTGRES_PR)
    process_source(sid_pg)

    with session_scope() as s:
        sid_mg = _seed(s, MONGO_PR)
    summary = process_source(sid_mg)

    assert summary["decisions"] >= 1, "stub should extract a Mongo decision"
    assert len(summary["findings"]) == 1, "exactly one contradiction expected"

    # The finding has full provenance: a row in conflict_findings, a row in
    # extractions for the verifier call, and a contradicts edge in the graph.
    with session_scope() as s:
        finding = s.execute(
            text(
                """
                SELECT cf.severity, cf.rationale, cf.extraction_id,
                       d.title AS prior_title, e.prompt_version
                FROM conflict_findings cf
                JOIN decisions d ON d.id = cf.conflicting_decision_id
                JOIN extractions e ON e.id = cf.extraction_id
                """
            )
        ).mappings().one()
        assert finding["severity"] == "high"
        assert "Postgres" in finding["prior_title"]
        assert finding["prompt_version"] == "contradiction_verifier.v1"

        edge = s.execute(
            text(
                """
                SELECT relation, confidence
                FROM edges
                WHERE relation = 'contradicts'
                """
            )
        ).mappings().one()
        assert edge["relation"] == "contradicts"
        assert 0.0 <= edge["confidence"] <= 1.0


def test_provenance_chain_is_complete():
    """Every node and edge must trace back to an extraction tied to a source."""
    with session_scope() as s:
        sid = _seed(s, POSTGRES_PR)
    process_source(sid)

    with session_scope() as s:
        # Every decision points to an extraction that points to a source.
        bad = s.execute(
            text(
                """
                SELECT d.id
                FROM decisions d
                LEFT JOIN extractions e ON e.id = d.extraction_id
                LEFT JOIN sources s ON s.id = e.source_id
                WHERE e.id IS NULL OR s.id IS NULL
                """
            )
        ).all()
        assert bad == [], f"orphan decisions: {bad}"

        bad_edges = s.execute(
            text(
                """
                SELECT id FROM edges
                WHERE extraction_id NOT IN (SELECT id FROM extractions)
                """
            )
        ).all()
        assert bad_edges == [], f"orphan edges: {bad_edges}"
