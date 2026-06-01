"""Pipeline shape tests using the stub LLM + stub embedder.

These don't verify LLM quality. They verify the pipeline wiring is correct:
  - Schema applies cleanly.
  - Extraction -> graph writer produces typed nodes + edges with provenance.
  - Contradiction detection finds the seeded Postgres↔MongoDB conflict.
  - Provenance click-through (decision -> extraction -> source) joins to the source.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from edm.db import session_scope
from edm.pipeline import process_source
from tests.helpers import SEED_MONGO, SEED_POSTGRES, insert_source


def _process(seed: dict):
    sid = insert_source(**seed)
    return sid, process_source(sid)


def test_schema_has_pgvector():
    with session_scope() as s:
        n = s.execute(text("SELECT COUNT(*) FROM pg_extension WHERE extname='vector'")).scalar_one()
    assert n == 1


def test_extraction_writes_decisions_with_provenance():
    sid, summary = _process(SEED_POSTGRES)
    assert summary["decisions"] == 1
    assert summary["constraints"] >= 1
    assert summary["assumptions"] >= 1
    # provenance: decisions.extraction_id -> extractions.source_id == sid
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT d.title, e.source_id
                FROM decisions d
                JOIN extractions e ON e.id = d.extraction_id
                """
            )
        ).all()
    assert len(rows) == 1
    assert rows[0][1] == sid
    assert "Postgres" in rows[0][0]


def test_edges_carry_extraction_id():
    _process(SEED_POSTGRES)
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT relation, extraction_id
                FROM edges
                """
            )
        ).all()
    # Every seeded edge must be backed by an extraction (provenance invariant).
    assert len(rows) >= 2
    assert all(r[1] is not None for r in rows)


def test_contradiction_detection_finds_postgres_mongo_conflict():
    _process(SEED_POSTGRES)
    sid_mongo, summary = _process(SEED_MONGO)
    assert len(summary["findings"]) == 1, summary
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT cf.severity, cf.rationale, d.title AS prior_title,
                       sp.title AS prior_source_title,
                       st.title AS triggering_title
                FROM conflict_findings cf
                JOIN decisions d ON d.id = cf.conflicting_decision_id
                JOIN extractions e ON e.id = d.extraction_id
                JOIN sources sp ON sp.id = e.source_id
                JOIN sources st ON st.id = cf.triggering_source_id
                """
            )
        ).mappings().first()
    assert row is not None
    assert "Postgres" in row["prior_title"]
    assert "MongoDB" in row["triggering_title"]
    assert row["severity"] in ("low", "medium", "high")


def test_contradicts_edge_is_written():
    _process(SEED_POSTGRES)
    _process(SEED_MONGO)
    with session_scope() as s:
        n = s.execute(
            text("SELECT COUNT(*) FROM edges WHERE relation='contradicts'")
        ).scalar_one()
    assert n == 1


def test_unrelated_source_produces_no_decisions():
    sid = insert_source(
        kind="pr_comment",
        external_id="demo/repo#1/comment/x",
        title="lgtm",
        body="lgtm, merging",
        days_ago=1,
    )
    summary = process_source(sid)
    assert summary["decisions"] == 0
    assert summary["findings"] == []
