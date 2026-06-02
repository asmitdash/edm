"""SOP generation session lifecycle.

A session walks through:
  open -> collecting_form -> chatting -> ready_to_generate -> generating ->
  completed (or abandoned)

The chatbot is iterative: each user turn extracts new exceptions and asks the
next-most-useful question. Form intake is structured + validated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope
from edm.sop.schemas import DerivedException, SOPFormData


@dataclass
class SessionRow:
    id: UUID
    function_slug: str | None
    function_id: UUID | None
    title: str | None
    org_label: str | None
    industry: str | None
    initiated_by: UUID | None
    status: str
    form_data: dict
    derived_exceptions: list[dict]
    retrieved_sop_ids: list[UUID]
    consent_to_train: bool
    generated_sop_id: UUID | None
    generated_skills_file_id: UUID | None
    created_at: datetime
    updated_at: datetime


def _row_to_session(r) -> SessionRow:
    return SessionRow(
        id=r["id"],
        function_slug=r["function_slug"],
        function_id=r["function_id"],
        title=r["title"],
        org_label=r["org_label"],
        industry=r["industry"],
        initiated_by=r["initiated_by"],
        status=r["status"],
        form_data=r["form_data"] or {},
        derived_exceptions=r["derived_exceptions"] or [],
        retrieved_sop_ids=list(r["retrieved_sop_ids"] or []),
        consent_to_train=r["consent_to_train"],
        generated_sop_id=r["generated_sop_id"],
        generated_skills_file_id=r["generated_skills_file_id"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _function_id_for_slug(s, slug: str | None) -> UUID | None:
    if not slug:
        return None
    return s.execute(text("SELECT id FROM functions WHERE slug = :s"), {"s": slug}).scalar_one_or_none()


def _select_session_sql() -> str:
    return """
        SELECT ss.id, ss.title, ss.org_label, ss.industry, ss.initiated_by, ss.status,
               ss.form_data, ss.derived_exceptions, ss.retrieved_sop_ids,
               ss.consent_to_train,
               ss.generated_sop_id, ss.generated_skills_file_id,
               ss.created_at, ss.updated_at,
               ss.function_id,
               f.slug AS function_slug
        FROM sop_sessions ss
        LEFT JOIN functions f ON f.id = ss.function_id
    """


def create_session(
    *,
    initiated_by: UUID,
    function_slug: str | None = None,
    title: str | None = None,
    org_label: str | None = None,
    industry: str | None = None,
    consent_to_train: bool = True,
) -> SessionRow:
    with session_scope() as s:
        fid = _function_id_for_slug(s, function_slug)
        new_id = s.execute(
            text(
                """
                INSERT INTO sop_sessions
                  (function_id, title, org_label, industry, initiated_by,
                   status, consent_to_train)
                VALUES (:fid, :title, :org, :ind, :u, 'collecting_form', :ct)
                RETURNING id
                """
            ),
            {"fid": fid, "title": title, "org": org_label, "ind": industry, "u": initiated_by, "ct": consent_to_train},
        ).scalar_one()
        row = s.execute(
            text(_select_session_sql() + " WHERE ss.id = :id"),
            {"id": new_id},
        ).mappings().one()
        return _row_to_session(row)


def get_session(session_id: UUID) -> SessionRow | None:
    with session_scope() as s:
        row = s.execute(
            text(_select_session_sql() + " WHERE ss.id = :id"),
            {"id": session_id},
        ).mappings().first()
        return _row_to_session(row) if row else None


def list_sessions(*, initiated_by: UUID | None = None, limit: int = 50) -> list[SessionRow]:
    with session_scope() as s:
        if initiated_by:
            rows = s.execute(
                text(_select_session_sql() + " WHERE ss.initiated_by = :u ORDER BY ss.updated_at DESC LIMIT :k"),
                {"u": initiated_by, "k": limit},
            ).mappings().all()
        else:
            rows = s.execute(
                text(_select_session_sql() + " ORDER BY ss.updated_at DESC LIMIT :k"),
                {"k": limit},
            ).mappings().all()
        return [_row_to_session(r) for r in rows]


def attach_form(session_id: UUID, form: SOPFormData) -> SessionRow:
    with session_scope() as s:
        fid = _function_id_for_slug(s, form.function_slug)
        s.execute(
            text(
                """
                UPDATE sop_sessions
                   SET form_data = CAST(:fd AS JSONB),
                       title = COALESCE(:title, title),
                       org_label = COALESCE(:org, org_label),
                       industry = COALESCE(:ind, industry),
                       function_id = COALESCE(:fid, function_id),
                       status = CASE WHEN status = 'collecting_form' THEN 'chatting' ELSE status END,
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {
                "fd": form.model_dump_json(),
                "title": form.title,
                "org": form.org_name,
                "ind": form.industry,
                "fid": fid,
                "id": session_id,
            },
        )
        row = s.execute(
            text(_select_session_sql() + " WHERE ss.id = :id"),
            {"id": session_id},
        ).mappings().one()
        return _row_to_session(row)


def append_chat_message(
    session_id: UUID,
    *,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    with session_scope() as s:
        s.execute(
            text(
                """
                INSERT INTO sop_session_messages (session_id, role, content, metadata)
                VALUES (:sid, :r, :c, CAST(:m AS JSONB))
                """
            ),
            {"sid": session_id, "r": role, "c": content, "m": json.dumps(metadata or {})},
        )
        s.execute(
            text("UPDATE sop_sessions SET updated_at = now() WHERE id = :id"),
            {"id": session_id},
        )


def add_derived_exceptions(session_id: UUID, new_exceptions: list[DerivedException]) -> None:
    if not new_exceptions:
        return
    with session_scope() as s:
        cur = s.execute(
            text("SELECT derived_exceptions FROM sop_sessions WHERE id = :id"),
            {"id": session_id},
        ).scalar_one()
        existing = list(cur or [])
        existing.extend([e.model_dump() for e in new_exceptions])
        s.execute(
            text(
                """
                UPDATE sop_sessions
                   SET derived_exceptions = CAST(:de AS JSONB), updated_at = now()
                 WHERE id = :id
                """
            ),
            {"de": json.dumps(existing), "id": session_id},
        )


def set_status(session_id: UUID, status: str) -> None:
    with session_scope() as s:
        s.execute(
            text("UPDATE sop_sessions SET status = :st, updated_at = now() WHERE id = :id"),
            {"st": status, "id": session_id},
        )


def get_messages(session_id: UUID) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT role, content, created_at
                FROM sop_session_messages
                WHERE session_id = :id
                ORDER BY created_at ASC
                """
            ),
            {"id": session_id},
        ).mappings().all()
        return [dict(r) for r in rows]


def attach_generated(
    session_id: UUID,
    *,
    sop_id: UUID,
    skills_file_id: UUID,
    retrieved_sop_ids: list[UUID],
) -> None:
    with session_scope() as s:
        s.execute(
            text(
                """
                UPDATE sop_sessions
                   SET status = 'completed',
                       generated_sop_id = :sop,
                       generated_skills_file_id = :sk,
                       retrieved_sop_ids = :rids,
                       completed_at = now(),
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"sop": sop_id, "sk": skills_file_id, "rids": retrieved_sop_ids, "id": session_id},
        )
