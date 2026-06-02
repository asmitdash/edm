"""Retrieval over the Company Brain corpus.

Two surfaces:
  - find_similar_procedures(query, function_slug?, top_k)  -> ranked procedures
  - find_similar_sops(query, function_slug?, top_k)        -> ranked SOPs

Both pgvector cosine. The SOP retrieval is what the SOP generator uses to seed
synthesis from precedent (customer's own SOPs ranked higher).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from edm.extract.embeddings import embed_one


@dataclass
class ProcedureHit:
    id: UUID
    title: str
    summary: str
    function_slug: str | None
    score: float


@dataclass
class SOPHit:
    id: UUID
    title: str
    body_markdown: str
    function_id: UUID | None
    source_kind: str
    org_label: str | None
    score: float


def _vector_literal(emb: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in emb) + "]"


def find_similar_procedures(
    session: Session,
    query: str,
    *,
    function_slug: str | None = None,
    top_k: int = 8,
) -> list[ProcedureHit]:
    if not query.strip():
        return []
    q_emb = embed_one(query, input_type="query")
    rows = session.execute(
        text(
            """
            SELECT p.id, p.title, p.summary,
                   f.slug AS function_slug,
                   1 - (p.embedding <=> CAST(:q AS vector)) AS score
            FROM procedures p
            LEFT JOIN functions f ON f.id = p.function_id
            WHERE p.embedding IS NOT NULL
              AND (:slug IS NULL OR f.slug = :slug)
              AND p.status = 'active'
            ORDER BY p.embedding <=> CAST(:q AS vector)
            LIMIT :k
            """
        ),
        {"q": _vector_literal(q_emb), "slug": function_slug, "k": top_k},
    ).mappings().all()
    return [ProcedureHit(**dict(r)) for r in rows]


def find_similar_sops(
    session: Session,
    query: str,
    *,
    function_slug: str | None = None,
    top_k: int = 6,
    prefer_uploaded: bool = True,
) -> list[SOPHit]:
    """Retrieve seed SOPs for synthesis.

    `prefer_uploaded=True` boosts customer-uploaded SOPs above seed templates so
    the customer's own corpus drives synthesis when present.
    """
    if not query.strip():
        return []
    q_emb = embed_one(query, input_type="query")
    rows = session.execute(
        text(
            """
            SELECT s.id, s.title, s.body_markdown,
                   s.function_id, s.source_kind, s.org_label,
                   (1 - (s.embedding <=> CAST(:q AS vector)))
                     + CASE
                         WHEN :prefer_uploaded AND s.source_kind = 'uploaded' THEN 0.05
                         WHEN s.source_kind = 'generated' THEN 0.02
                         ELSE 0
                       END AS score
            FROM sops s
            LEFT JOIN functions f ON f.id = s.function_id
            WHERE s.embedding IS NOT NULL
              AND (:slug IS NULL OR f.slug = :slug)
            ORDER BY score DESC
            LIMIT :k
            """
        ),
        {
            "q": _vector_literal(q_emb),
            "slug": function_slug,
            "k": top_k,
            "prefer_uploaded": prefer_uploaded,
        },
    ).mappings().all()
    return [SOPHit(**dict(r)) for r in rows]
