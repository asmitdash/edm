"""Procedure-level contradiction detector (Company Brain).

Mirrors `reason.contradictions` for the procedure graph. Two-stage:
  1. embedding retrieval finds candidate prior procedures
  2. LLM verifier flips precision high

Use cases:
  * a new policy doc lands that reverses a refund-handling procedure
  * a leadership email contradicts the existing escalation chain
  * a meeting transcript captures a process change that conflicts with the wiki
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from edm.extract.providers import LLMProvider, get_llm_provider
from edm.logging import get_logger

log = get_logger(__name__)

PROC_VERIFIER_PROMPT_VERSION = "procedure_contradiction_verifier.v1"

_PROC_VERIFIER_SYSTEM = """\
You are a strict verifier. You will be shown two business procedures and asked \
whether the NEW procedure genuinely contradicts the PRIOR procedure.

A genuine contradiction means: following the new procedure would invalidate, \
reverse, or directly conflict with the prior one. Same-topic is not contradiction. \
Refinement, extension, role change, or step reordering that does not change the \
outcome are NOT contradictions. Only flag conflicts where the two procedures cannot \
both be true at the same time.

Default to non-contradicting on uncertainty. False positives are far worse than \
false negatives.

Respond ONLY via the schema.
"""

_PROC_VERDICT_SCHEMA: dict[str, Any] = {
    "name": "report_procedure_verdict",
    "description": "Report whether the new procedure contradicts the prior one.",
    "input_schema": {
        "type": "object",
        "properties": {
            "contradicts": {"type": "boolean"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "rationale": {"type": "string"},
        },
        "required": ["contradicts", "severity", "rationale"],
    },
}


def _vec_literal(emb: Any) -> str:
    if isinstance(emb, str):
        return emb
    if isinstance(emb, (list, tuple)):
        return "[" + ",".join(f"{float(x):.7f}" for x in emb) + "]"
    return str(emb)


def detect_for_procedure(
    session: Session,
    procedure_id: UUID,
    *,
    triggering_source_id: UUID,
    top_k: int = 8,
    similarity_threshold: float = 0.05,
    provider: LLMProvider | None = None,
) -> list[UUID]:
    """Find prior procedures semantically similar to `procedure_id`, ask the LLM
    which (if any) it contradicts, and write findings. Returns finding ids."""
    provider = provider or get_llm_provider()

    row = session.execute(
        text(
            """
            SELECT p.id, p.title, p.summary, p.embedding,
                   f.slug AS function_slug
            FROM procedures p
            LEFT JOIN functions f ON f.id = p.function_id
            WHERE p.id = :id
            """
        ),
        {"id": procedure_id},
    ).mappings().first()
    if row is None or row["embedding"] is None:
        return []

    candidates = session.execute(
        text(
            """
            SELECT p.id, p.title, p.summary,
                   1 - (p.embedding <=> CAST(:emb AS vector)) AS similarity,
                   f.slug AS function_slug
            FROM procedures p
            LEFT JOIN functions f ON f.id = p.function_id
            WHERE p.id != :self_id
              AND p.status = 'active'
              AND p.embedding IS NOT NULL
            ORDER BY p.embedding <=> CAST(:emb AS vector)
            LIMIT :k
            """
        ),
        {"emb": _vec_literal(row["embedding"]), "self_id": procedure_id, "k": top_k},
    ).mappings().all()

    finding_ids: list[UUID] = []
    for cand in candidates:
        if float(cand["similarity"]) < similarity_threshold:
            continue

        prompt = (
            f"PRIOR PROCEDURE\n"
            f"Function: {cand['function_slug'] or '—'}\n"
            f"Title: {cand['title']}\nSummary: {cand['summary']}\n\n"
            f"NEW PROCEDURE\n"
            f"Function: {row['function_slug'] or '—'}\n"
            f"Title: {row['title']}\nSummary: {row['summary']}\n\n"
            "Does the NEW procedure contradict the PRIOR procedure?"
        )
        result = provider.verify(system=_PROC_VERIFIER_SYSTEM, user=prompt, schema=_PROC_VERDICT_SCHEMA)
        verdict = result.payload
        if not bool(verdict.get("contradicts")):
            continue

        verifier_extraction_id = session.execute(
            text(
                """
                INSERT INTO extractions
                  (source_id, prompt_version, model, raw_response, input_tokens, output_tokens)
                VALUES (:source_id, :pv, :model, CAST(:raw AS JSONB), :in_tok, :out_tok)
                RETURNING id
                """
            ),
            {
                "source_id": triggering_source_id,
                "pv": PROC_VERIFIER_PROMPT_VERSION,
                "model": result.model,
                "raw": json.dumps(result.raw_response),
                "in_tok": result.input_tokens,
                "out_tok": result.output_tokens,
            },
        ).scalar_one()

        finding_id = session.execute(
            text(
                """
                INSERT INTO procedure_conflict_findings
                  (triggering_source_id, conflicting_procedure_id, new_procedure_id,
                   severity, rationale, extraction_id)
                VALUES (:tsi, :cpi, :npi, :sev, :rat, :ex)
                RETURNING id
                """
            ),
            {
                "tsi": triggering_source_id,
                "cpi": cand["id"],
                "npi": procedure_id,
                "sev": verdict.get("severity", "medium"),
                "rat": verdict.get("rationale", ""),
                "ex": verifier_extraction_id,
            },
        ).scalar_one()

        session.execute(
            text(
                """
                INSERT INTO edges
                  (src_kind, src_id, dst_kind, dst_id, relation, confidence, extraction_id)
                VALUES ('procedure', :new_id, 'procedure', :prior_id, 'contradicts', :conf, :ex)
                ON CONFLICT (src_kind, src_id, dst_kind, dst_id, relation) DO NOTHING
                """
            ),
            {
                "new_id": procedure_id,
                "prior_id": cand["id"],
                "conf": 0.6 if verdict.get("severity") == "low" else 0.85,
                "ex": verifier_extraction_id,
            },
        )

        finding_ids.append(finding_id)
        log.info(
            "procedure_contradiction.found",
            new_procedure_id=str(procedure_id),
            prior_procedure_id=str(cand["id"]),
            severity=verdict.get("severity"),
            similarity=float(cand["similarity"]),
        )

    return finding_ids
