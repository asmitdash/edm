"""Graph writer.

Takes an `ExtractionRun` and writes:
  1. The extraction row (audit trail)
  2. Decision / assumption / constraint / alternative rows
  3. Edges between them (resolving local_ids -> uuids)

All within one transaction so partial failures do not corrupt the graph.

Provenance is wired here: every node and edge is stamped with the extraction_id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from edm.extract.embeddings import embed_texts
from edm.extract.extractor import ExtractionRun
from edm.logging import get_logger

log = get_logger(__name__)


@dataclass
class WriteResult:
    extraction_id: UUID
    decision_ids: dict[str, UUID]
    assumption_ids: dict[str, UUID]
    constraint_ids: dict[str, UUID]
    alternative_ids: dict[str, UUID]
    edges_written: int


_KIND_TO_TABLE = {
    "decision": "decisions",
    "assumption": "assumptions",
    "constraint": "constraints",
    "alternative": "decision_alternatives",
}


def write_extraction(session: Session, source_id: UUID, run: ExtractionRun) -> WriteResult:
    """Persist a single ExtractionRun. Idempotency is the caller's job (don't
    re-run extraction on the same source unless you want duplicates)."""

    # 1. extraction row
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

    result = WriteResult(extraction_id, {}, {}, {}, {}, 0)

    # 2. nodes — combine into one Voyage call to stay under free-tier RPM limits
    decision_texts = [f"{d.title}\n\n{d.summary}" for d in run.result.decisions]
    assumption_texts = [a.statement for a in run.result.assumptions]
    constraint_texts = [c.statement for c in run.result.constraints]
    nd, na, nc = len(decision_texts), len(assumption_texts), len(constraint_texts)
    all_texts = decision_texts + assumption_texts + constraint_texts
    all_embs = embed_texts(all_texts) if all_texts else []
    decision_embs = all_embs[:nd]
    assumption_embs = all_embs[nd:nd + na]
    constraint_embs = all_embs[nd + na:nd + na + nc]

    for d, emb in zip(run.result.decisions, decision_embs):
        new_id = session.execute(
            text(
                """
                INSERT INTO decisions (title, summary, extraction_id, embedding, metadata)
                VALUES (:title, :summary, :extraction_id, CAST(:emb AS vector),
                        CAST(:meta AS JSONB))
                RETURNING id
                """
            ),
            {
                "title": d.title,
                "summary": d.summary,
                "extraction_id": extraction_id,
                "emb": _vector_literal(emb),
                "meta": json.dumps({"quoted_evidence": d.quoted_evidence}),
            },
        ).scalar_one()
        result.decision_ids[d.local_id] = new_id

    for a, emb in zip(run.result.assumptions, assumption_embs):
        new_id = session.execute(
            text(
                """
                INSERT INTO assumptions (statement, extraction_id, embedding)
                VALUES (:statement, :extraction_id, CAST(:emb AS vector))
                RETURNING id
                """
            ),
            {"statement": a.statement, "extraction_id": extraction_id, "emb": _vector_literal(emb)},
        ).scalar_one()
        result.assumption_ids[a.local_id] = new_id

    for c, emb in zip(run.result.constraints, constraint_embs):
        new_id = session.execute(
            text(
                """
                INSERT INTO constraints (statement, kind, extraction_id, embedding)
                VALUES (:statement, :kind, :extraction_id, CAST(:emb AS vector))
                RETURNING id
                """
            ),
            {
                "statement": c.statement,
                "kind": c.kind,
                "extraction_id": extraction_id,
                "emb": _vector_literal(emb),
            },
        ).scalar_one()
        result.constraint_ids[c.local_id] = new_id

    for alt in run.result.alternatives:
        decision_uuid = result.decision_ids.get(alt.decision_local_id)
        if decision_uuid is None:
            log.warning(
                "graph.alternative_unresolved",
                local_id=alt.local_id,
                decision_local_id=alt.decision_local_id,
            )
            continue
        new_id = session.execute(
            text(
                """
                INSERT INTO decision_alternatives
                  (decision_id, description, rejection_reason, extraction_id)
                VALUES (:decision_id, :description, :rejection_reason, :extraction_id)
                RETURNING id
                """
            ),
            {
                "decision_id": decision_uuid,
                "description": alt.description,
                "rejection_reason": alt.rejection_reason,
                "extraction_id": extraction_id,
            },
        ).scalar_one()
        result.alternative_ids[alt.local_id] = new_id

    # 3. edges
    local_to = {
        **{lid: ("decision", uid) for lid, uid in result.decision_ids.items()},
        **{lid: ("assumption", uid) for lid, uid in result.assumption_ids.items()},
        **{lid: ("constraint", uid) for lid, uid in result.constraint_ids.items()},
        **{lid: ("alternative", uid) for lid, uid in result.alternative_ids.items()},
    }

    for e in run.result.edges:
        src = local_to.get(e.src_local_id)
        dst = local_to.get(e.dst_local_id)
        if src is None or dst is None:
            log.warning("graph.edge_unresolved", src=e.src_local_id, dst=e.dst_local_id)
            continue
        session.execute(
            text(
                """
                INSERT INTO edges
                  (src_kind, src_id, dst_kind, dst_id, relation, confidence, extraction_id)
                VALUES (:sk, :si, :dk, :di, :rel, :conf, :ex)
                ON CONFLICT (src_kind, src_id, dst_kind, dst_id, relation) DO NOTHING
                """
            ),
            {
                "sk": src[0],
                "si": src[1],
                "dk": dst[0],
                "di": dst[1],
                "rel": e.relation,
                "conf": e.confidence,
                "ex": extraction_id,
            },
        )
        result.edges_written += 1

    log.info(
        "graph.write_complete",
        extraction_id=str(extraction_id),
        decisions=len(result.decision_ids),
        assumptions=len(result.assumption_ids),
        constraints=len(result.constraint_ids),
        alternatives=len(result.alternative_ids),
        edges=result.edges_written,
    )
    return result


def _vector_literal(emb: list[float]) -> str:
    """pgvector accepts a string-formatted vector literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.7f}" for x in emb) + "]"
