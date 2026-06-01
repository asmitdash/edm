"""Append-only audit log. Admin-visible only."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope


def log(*, user_id: UUID | None, action: str, target: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO audit_log (user_id, action, target, metadata) "
                "VALUES (:u, :a, :t, CAST(:m AS JSONB))"
            ),
            {"u": user_id, "a": action, "t": target, "m": json.dumps(metadata or {})},
        )


def recent(limit: int = 100) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT al.created_at, al.action, al.target, al.metadata,
                       u.username
                FROM audit_log al
                LEFT JOIN users u ON u.id = al.user_id
                ORDER BY al.created_at DESC
                LIMIT :l
                """
            ),
            {"l": limit},
        ).mappings().all()
    return [dict(r) for r in rows]
