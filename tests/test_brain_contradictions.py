"""Procedure-level contradiction detection tests.

Mirrors test_pipeline_offline.py but for the Brain layer. Two refund-handling
procedures that disagree on the manager-approval guardrail should produce a
contradiction finding when the second one lands.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from edm.db import session_scope
from edm.pipeline import process_source


REFUND_PRIOR = {
    "kind": "policy_doc",
    "external_id": "policy/refund-prior",
    "title": "Refund handling — current",
    "body": (
        "When a customer requests a refund, the support agent verifies the customer "
        "in the billing system, confirms the refund window, and processes via the "
        "billing tool. Refunds over $500 require manager approval. Always log the "
        "refund event in the CRM with a reason code."
    ),
    "author": "ops",
    "days_ago": 60,
}

REFUND_NEW = {
    "kind": "policy_doc",
    "external_id": "policy/refund-new",
    "title": "Refund handling — updated",
    "body": (
        "Refund process update. Refunds can now be issued without manager approval "
        "by any support agent regardless of amount. Verify customer in billing and "
        "process via the billing tool. CRM log still required."
    ),
    "author": "ops",
    "days_ago": 1,
}


def _seed(s, src):
    occurred_at = datetime.now(timezone.utc) - timedelta(days=src["days_ago"])
    return s.execute(
        text(
            """
            INSERT INTO sources
              (kind, external_id, title, body, author, occurred_at, content_hash, metadata)
            VALUES (:k, :e, :t, :b, :a, :ts, :h, CAST(:m AS JSONB))
            RETURNING id
            """
        ),
        {
            "k": src["kind"], "e": src["external_id"],
            "t": src["title"], "b": src["body"], "a": src["author"],
            "ts": occurred_at,
            "h": hashlib.sha256(src["body"].encode()).hexdigest(),
            "m": json.dumps({"seeded": True}),
        },
    ).scalar_one()


def test_procedure_contradiction_detected_on_policy_update():
    with session_scope() as s:
        sid_prior = _seed(s, REFUND_PRIOR)
    summary_prior = process_source(sid_prior)
    assert summary_prior["procedures"] >= 1

    with session_scope() as s:
        sid_new = _seed(s, REFUND_NEW)
    summary_new = process_source(sid_new)
    assert summary_new["procedures"] >= 1
    assert "procedure_findings" in summary_new
    # The stub verifier flips contradicts=True when the new procedure's body
    # contains "no manager approval" / "without manager approval".
    assert len(summary_new["procedure_findings"]) >= 1, (
        f"expected at least one procedure contradiction, got: {summary_new}"
    )

    with session_scope() as s:
        finding = s.execute(
            text(
                """
                SELECT pcf.severity, pcf.rationale,
                       p_prior.title AS prior_title,
                       p_new.title AS new_title
                FROM procedure_conflict_findings pcf
                JOIN procedures p_prior ON p_prior.id = pcf.conflicting_procedure_id
                JOIN procedures p_new ON p_new.id = pcf.new_procedure_id
                """
            )
        ).mappings().first()
        assert finding is not None
        assert finding["severity"] == "high"

        # contradicts edge written between procedure nodes
        edge = s.execute(
            text(
                """
                SELECT relation FROM edges
                WHERE relation = 'contradicts'
                  AND src_kind = 'procedure' AND dst_kind = 'procedure'
                """
            )
        ).mappings().first()
        assert edge is not None
