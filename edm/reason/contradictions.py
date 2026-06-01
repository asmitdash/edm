"""Contradiction detector.

Pipeline (the demo's headline moment):
  1. A new source (PR, doc) is ingested and extracted, producing N new decisions.
  2. For each new decision, embedding retrieval surfaces the K most similar prior
     decisions (excluding the same source).
  3. For each candidate above threshold, the LLM verifier returns yes/no with
     rationale and severity. Default to non-contradiction on uncertainty.
  4. Confirmed contradictions become rows in `conflict_findings` and a
     `contradicts` edge in the graph. All carry provenance via extraction_id.

Why two-stage: embeddings find candidates cheaply; the LLM verifier flips
precision high so we don't post noisy PR comments.
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

VERIFIER_PROMPT_VERSION = "contradiction_verifier.v1"

_VERIFIER_SYSTEM = """\
You are a strict verifier. You will be shown two engineering decisions and asked \
whether the NEW decision genuinely contradicts the PRIOR decision.

A genuine contradiction means: implementing the new decision would invalidate, reverse, \
or directly conflict with the prior decision. Same-topic is not contradiction. Refinement, \
extension, or adjustment is not contradiction.

Respond ONLY via the schema. Default to non-contradicting when uncertain — false positives \
are far worse than false negatives in this pipeline.
"""

_VERDICT_SCHEMA: dict[str, Any] = {
    "name": "report_verdict",
    "description": "Report whether the new decision contradicts the prior decision.",
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


def detect_for_decision(
    session: Session,
    decision_id: UUID,
    *,
    triggering_source_id: UUID,
    top_k: int = 8,
    similarity_threshold: float = 0.05,
    provider: LLMProvider | None = None,
) -> list[UUID]:
    """Find prior decisions semantically similar to `decision_id`, ask the LLM
    which (if any) it contradicts, and write findings. Returns finding ids.

    similarity_threshold defaults to 0.25 — this is intentionally generous because
    the LLM verifier filters false positives. Tune up if verifier costs hurt.
    """
    provider = provider or get_llm_provider()

    row = session.execute(
        text(
            """
            SELECT id, title, summary, embedding
            FROM decisions
            WHERE id = :id
            """
        ),
        {"id": decision_id},
    ).mappings().first()
    if row is None or row["embedding"] is None:
        return []

    candidates = session.execute(
        text(
            """
            SELECT id, title, summary,
                   1 - (embedding <=> CAST(:emb AS vector)) AS similarity
            FROM decisions
            WHERE id != :self_id
              AND status = 'active'
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:emb AS vector)
            LIMIT :k
            """
        ),
        {"emb": _vec_literal_from_pg(row["embedding"]), "self_id": decision_id, "k": top_k},
    ).mappings().all()

    finding_ids: list[UUID] = []
    for cand in candidates:
        if float(cand["similarity"]) < similarity_threshold:
            continue

        prompt = (
            f"PRIOR DECISION\nTitle: {cand['title']}\nSummary: {cand['summary']}\n\n"
            f"NEW DECISION\nTitle: {row['title']}\nSummary: {row['summary']}\n\n"
            "Does the NEW decision contradict the PRIOR decision?"
        )
        result = provider.verify(system=_VERIFIER_SYSTEM, user=prompt, schema=_VERDICT_SCHEMA)
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
                "pv": VERIFIER_PROMPT_VERSION,
                "model": result.model,
                "raw": json.dumps(result.raw_response),
                "in_tok": result.input_tokens,
                "out_tok": result.output_tokens,
            },
        ).scalar_one()

        finding_id = session.execute(
            text(
                """
                INSERT INTO conflict_findings
                  (triggering_source_id, conflicting_decision_id, severity, rationale, extraction_id)
                VALUES (:tsi, :cdi, :sev, :rat, :ex)
                RETURNING id
                """
            ),
            {
                "tsi": triggering_source_id,
                "cdi": cand["id"],
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
                VALUES ('decision', :new_id, 'decision', :prior_id, 'contradicts', :conf, :ex)
                ON CONFLICT (src_kind, src_id, dst_kind, dst_id, relation) DO NOTHING
                """
            ),
            {
                "new_id": decision_id,
                "prior_id": cand["id"],
                "conf": 0.6 if verdict.get("severity") == "low" else 0.85,
                "ex": verifier_extraction_id,
            },
        )

        finding_ids.append(finding_id)
        log.info(
            "contradiction.found",
            new_decision_id=str(decision_id),
            prior_decision_id=str(cand["id"]),
            severity=verdict.get("severity"),
            similarity=float(cand["similarity"]),
        )

    return finding_ids


def _vec_literal_from_pg(emb: Any) -> str:
    """Postgres returns the embedding column as a list (via pgvector adapter) or
    as the vector string literal. Normalise to a literal string for the
    parametrized query."""
    if isinstance(emb, str):
        return emb
    if isinstance(emb, (list, tuple)):
        return "[" + ",".join(f"{float(x):.7f}" for x in emb) + "]"
    return str(emb)
