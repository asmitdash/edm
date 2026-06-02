"""Skills file helpers (read / serve / regenerate)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope


@dataclass
class SkillsFile:
    id: UUID
    sop_id: UUID
    version: int
    schema_version: str
    body: dict
    checksum: str
    created_at: datetime


def get_skills_file(skills_file_id: UUID) -> SkillsFile | None:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT id, sop_id, version, schema_version, body, checksum, created_at
                FROM skills_files WHERE id = :id
                """
            ),
            {"id": skills_file_id},
        ).mappings().first()
        return SkillsFile(**dict(row)) if row else None


def latest_for_sop(sop_id: UUID) -> SkillsFile | None:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT id, sop_id, version, schema_version, body, checksum, created_at
                FROM skills_files
                WHERE sop_id = :sop
                ORDER BY version DESC
                LIMIT 1
                """
            ),
            {"sop": sop_id},
        ).mappings().first()
        return SkillsFile(**dict(row)) if row else None
