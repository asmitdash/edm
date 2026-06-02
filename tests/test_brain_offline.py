"""Offline tests for the Company Brain procedure pipeline.

Verifies routing: a brain-kind source goes through procedure extraction +
write, lands rows in `procedures`, `procedure_steps`, `procedure_guardrails`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from edm.db import session_scope
from edm.pipeline import process_source


SUPPORT_TICKET = {
    "kind": "support_ticket",
    "external_id": "zd/12345",
    "title": "Refund handling — internal SOP draft",
    "body": (
        "When a customer requests a refund, the support agent verifies the customer "
        "in the billing system, confirms the refund window, and processes via the "
        "billing tool. Refunds over $500 require manager approval. Always log the "
        "refund event in the CRM with a reason code."
    ),
    "author": "lisa",
    "days_ago": 2,
}


def _seed(s, src: dict):
    occurred_at = datetime.now(timezone.utc) - timedelta(days=src["days_ago"])
    row = s.execute(
        text(
            """
            INSERT INTO sources
              (kind, external_id, title, body, author, occurred_at, content_hash, metadata)
            VALUES (:kind, :ext, :title, :body, :author, :occurred_at, :hash, CAST(:meta AS JSONB))
            RETURNING id
            """
        ),
        {
            "kind": src["kind"], "ext": src["external_id"],
            "title": src["title"], "body": src["body"], "author": src["author"],
            "occurred_at": occurred_at,
            "hash": hashlib.sha256(src["body"].encode()).hexdigest(),
            "meta": json.dumps({"seeded": True}),
        },
    ).mappings().first()
    return row["id"]


def test_brain_kind_routes_to_procedure_extraction():
    with session_scope() as s:
        sid = _seed(s, SUPPORT_TICKET)
    summary = process_source(sid)

    assert "procedures" in summary, "brain pipeline should report procedures"
    assert summary["procedures"] >= 1
    assert summary["steps"] >= 1
    assert summary["guardrails"] >= 1

    with session_scope() as s:
        rows = s.execute(
            text("SELECT title, function_id FROM procedures")
        ).mappings().all()
        assert any("Refund" in r["title"] or "refund" in r["title"].lower() for r in rows)
        steps = s.execute(text("SELECT COUNT(*) FROM procedure_steps")).scalar_one()
        guards = s.execute(text("SELECT COUNT(*) FROM procedure_guardrails")).scalar_one()
        assert steps >= 1
        assert guards >= 1


def test_brain_provenance_chain():
    with session_scope() as s:
        sid = _seed(s, SUPPORT_TICKET)
    process_source(sid)

    with session_scope() as s:
        bad = s.execute(
            text(
                """
                SELECT p.id FROM procedures p
                LEFT JOIN extractions e ON e.id = p.extraction_id
                LEFT JOIN sources src ON src.id = e.source_id
                WHERE e.id IS NULL OR src.id IS NULL
                """
            )
        ).all()
        assert bad == [], f"orphan procedures: {bad}"
