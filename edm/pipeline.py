"""End-to-end pipeline: source -> extraction -> graph -> contradiction detection.

Use this as the single entry point that callers (CLI, webhook handlers) invoke
for "process this source row" — it keeps orchestration in one place rather than
scattered across the surfaces.

Routing:
  * engineering kinds (pr, commit, design_doc, adr, *_upload) -> decision extraction
  * company-brain kinds (email_thread, support_ticket, crm_note, meeting_transcript,
    wiki_page, policy_doc, sop_*) -> procedure extraction
  * unknown kinds default to decision extraction (back-compat)
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from edm.brain.extractor import extract_procedures_from_source
from edm.brain.writer import write_procedures
from edm.db import session_scope
from edm.extract.extractor import extract_from_source
from edm.graph.writer import write_extraction
from edm.logging import get_logger
from edm.reason.contradictions import detect_for_decision
from edm.reason.procedure_contradictions import detect_for_procedure

log = get_logger(__name__)


_BRAIN_KINDS = {
    "email_thread", "support_ticket", "crm_note", "meeting_transcript",
    "wiki_page", "policy_doc",
    "sop_seed", "sop_uploaded", "sop_generated",
}


def process_source(source_id: UUID) -> dict:
    """Run extraction + graph write + contradiction detection on one source.
    Returns a summary dict suitable for logging or PR-comment building."""

    with session_scope() as session:
        row = session.execute(
            text(
                """
                SELECT id, kind, title, body, author, occurred_at
                FROM sources
                WHERE id = :id
                """
            ),
            {"id": source_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Source {source_id} not found")

        kind = row["kind"]

        if kind in _BRAIN_KINDS:
            return _process_for_brain(session, source_id, row)

        return _process_for_decisions(session, source_id, row)


def _process_for_decisions(session, source_id: UUID, row) -> dict:
    run = extract_from_source(
        kind=row["kind"],
        title=row["title"],
        author=row["author"],
        occurred_at=row["occurred_at"],
        body=row["body"],
    )
    if run.result.is_empty():
        log.info("pipeline.no_decisions", source_id=str(source_id))
        return {"source_id": str(source_id), "decisions": 0, "findings": []}

    write = write_extraction(session, source_id, run)

    all_findings: list[UUID] = []
    for new_decision_id in write.decision_ids.values():
        findings = detect_for_decision(session, new_decision_id, triggering_source_id=source_id)
        all_findings.extend(findings)

    return {
        "source_id": str(source_id),
        "extraction_id": str(write.extraction_id),
        "decisions": len(write.decision_ids),
        "assumptions": len(write.assumption_ids),
        "constraints": len(write.constraint_ids),
        "edges": write.edges_written,
        "findings": [str(f) for f in all_findings],
    }


def _process_for_brain(session, source_id: UUID, row) -> dict:
    run = extract_procedures_from_source(
        kind=row["kind"],
        title=row["title"],
        author=row["author"],
        occurred_at=row["occurred_at"],
        body=row["body"],
    )
    if run.result.is_empty():
        log.info("pipeline.no_procedures", source_id=str(source_id))
        return {"source_id": str(source_id), "procedures": 0}

    write = write_procedures(session, source_id, run)

    all_findings: list[UUID] = []
    for new_proc_id in write.procedure_ids.values():
        findings = detect_for_procedure(session, new_proc_id, triggering_source_id=source_id)
        all_findings.extend(findings)

    return {
        "source_id": str(source_id),
        "extraction_id": str(write.extraction_id),
        "procedures": len(write.procedure_ids),
        "steps": write.n_steps,
        "guardrails": write.n_guardrails,
        "procedure_findings": [str(f) for f in all_findings],
    }


def process_unprocessed(limit: int = 50) -> list[dict]:
    """Find sources without extractions and process them. Useful for backfills."""
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT s.id
                FROM sources s
                LEFT JOIN extractions e ON e.source_id = s.id
                WHERE e.id IS NULL
                ORDER BY s.occurred_at ASC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).all()
        ids = [r[0] for r in rows]

    summaries = []
    for sid in ids:
        try:
            summaries.append(process_source(sid))
        except Exception as e:
            log.error("pipeline.error", source_id=str(sid), error=str(e))
    return summaries
