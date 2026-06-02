"""Persist a ProcedureExtractionRun -> procedures + procedure_steps + procedure_guardrails.

Provenance: every procedure row carries the extraction_id; every step/guardrail
inherits via procedure_id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from edm.brain.extractor import ProcedureExtractionRun
from edm.extract.embeddings import embed_texts
from edm.logging import get_logger

log = get_logger(__name__)


@dataclass
class ProcedureWriteResult:
    extraction_id: UUID
    procedure_ids: dict[str, UUID] = field(default_factory=dict)
    n_steps: int = 0
    n_guardrails: int = 0


_VALID_FUNCTIONS = {
    "engineering", "product", "sales", "support", "success", "marketing",
    "finance", "hr", "legal", "it", "operations", "security",
}


def _vector_literal(emb: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in emb) + "]"


def write_procedures(
    session: Session,
    source_id: UUID,
    run: ProcedureExtractionRun,
    *,
    sop_id: UUID | None = None,
) -> ProcedureWriteResult:
    extraction_id = session.execute(
        text(
            """
            INSERT INTO extractions
              (source_id, prompt_version, model, raw_response, input_tokens, output_tokens)
            VALUES
              (:source_id, :prompt_version, :model, CAST(:raw_response AS JSONB), :in_tok, :out_tok)
            RETURNING id
            """
        ),
        {
            "source_id": source_id,
            "prompt_version": run.prompt_version,
            "model": run.model,
            "raw_response": json.dumps(run.raw_response),
            "in_tok": run.input_tokens,
            "out_tok": run.output_tokens,
        },
    ).scalar_one()

    result = ProcedureWriteResult(extraction_id=extraction_id)

    proc_texts = [f"{p.title}\n\n{p.summary}" for p in run.result.procedures]
    proc_embs = embed_texts(proc_texts) if proc_texts else []

    for proc, emb in zip(run.result.procedures, proc_embs):
        slug = proc.function_slug if proc.function_slug in _VALID_FUNCTIONS else "operations"
        function_id = session.execute(
            text("SELECT id FROM functions WHERE slug = :s"), {"s": slug}
        ).scalar_one_or_none()

        new_id = session.execute(
            text(
                """
                INSERT INTO procedures
                  (function_id, title, summary, trigger_description, owner_role,
                   extraction_id, sop_id, status, embedding, metadata)
                VALUES
                  (:fid, :title, :summary, :trigger, :owner,
                   :ex, :sop_id, 'active', CAST(:emb AS vector), CAST(:meta AS JSONB))
                RETURNING id
                """
            ),
            {
                "fid": function_id,
                "title": proc.title,
                "summary": proc.summary,
                "trigger": proc.trigger_description,
                "owner": proc.owner_role,
                "ex": extraction_id,
                "sop_id": sop_id,
                "emb": _vector_literal(emb),
                "meta": json.dumps({"function_slug": slug}),
            },
        ).scalar_one()
        result.procedure_ids[proc.local_id] = new_id

        for step in proc.steps:
            session.execute(
                text(
                    """
                    INSERT INTO procedure_steps
                      (procedure_id, step_index, instruction, actor_role,
                       tool_or_system, expected_outcome)
                    VALUES (:pid, :i, :ins, :ar, :ts, :eo)
                    ON CONFLICT (procedure_id, step_index) DO NOTHING
                    """
                ),
                {
                    "pid": new_id,
                    "i": step.step_index,
                    "ins": step.instruction,
                    "ar": step.actor_role,
                    "ts": step.tool_or_system,
                    "eo": step.expected_outcome,
                },
            )
            result.n_steps += 1

        for g in proc.guardrails:
            session.execute(
                text(
                    """
                    INSERT INTO procedure_guardrails
                      (procedure_id, kind, statement, condition_expr, severity)
                    VALUES (:pid, :k, :s, :c, :sev)
                    """
                ),
                {
                    "pid": new_id,
                    "k": g.kind,
                    "s": g.statement,
                    "c": g.condition_expr,
                    "sev": g.severity,
                },
            )
            result.n_guardrails += 1

    log.info(
        "brain.write_complete",
        extraction_id=str(extraction_id),
        procedures=len(result.procedure_ids),
        steps=result.n_steps,
        guardrails=result.n_guardrails,
    )
    return result
