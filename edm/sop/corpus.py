"""SOP corpus management: seed library + customer uploads + generated SOPs.

All three live in the same `sops` table, distinguished by `source_kind`. The
retrieval boost (uploaded > generated > seed) is applied in `brain.retrieval`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope
from edm.extract.embeddings import embed_one


@dataclass
class SOPRow:
    id: UUID
    title: str
    function_slug: str | None
    source_kind: str
    org_label: str | None
    industry: str | None
    confidentiality: str
    body_markdown: str
    created_at: datetime
    updated_at: datetime


def _vec_lit(emb: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in emb) + "]"


def _function_id(s, slug: str | None) -> UUID | None:
    if not slug:
        return None
    return s.execute(text("SELECT id FROM functions WHERE slug = :s"), {"s": slug}).scalar_one_or_none()


def upload_sop(
    *,
    title: str,
    body_markdown: str,
    function_slug: str | None,
    org_label: str | None,
    industry: str | None,
    confidentiality: str = "internal",
    created_by: UUID | None = None,
    source_id: UUID | None = None,
) -> UUID:
    """Insert a customer-uploaded SOP. Re-uploads with the same (kind,title,org)
    overwrite the body."""
    emb = embed_one(f"{title}\n\n{body_markdown}")
    with session_scope() as s:
        fid = _function_id(s, function_slug)
        new_id = s.execute(
            text(
                """
                INSERT INTO sops
                  (function_id, title, body_markdown, source_kind, source_id,
                   org_label, industry, confidentiality, embedding, metadata, created_by)
                VALUES
                  (:fid, :t, :b, 'uploaded', :src,
                   :org, :ind, :cf, CAST(:e AS vector), CAST(:m AS JSONB), :u)
                ON CONFLICT (source_kind, title, org_label) DO UPDATE
                  SET body_markdown = EXCLUDED.body_markdown,
                      embedding = EXCLUDED.embedding,
                      function_id = EXCLUDED.function_id,
                      industry = EXCLUDED.industry,
                      confidentiality = EXCLUDED.confidentiality,
                      updated_at = now()
                RETURNING id
                """
            ),
            {
                "fid": fid,
                "t": title,
                "b": body_markdown,
                "src": source_id,
                "org": org_label,
                "ind": industry,
                "cf": confidentiality,
                "e": _vec_lit(emb),
                "m": json.dumps({"function_slug": function_slug}),
                "u": created_by,
            },
        ).scalar_one()
    return new_id


def insert_seed_sop(
    *,
    title: str,
    body_markdown: str,
    function_slug: str | None,
    industry: str | None = None,
    org_label: str = "_seed",
) -> UUID:
    """Same shape as upload_sop but tagged as 'seed' so retrieval ranks it lower
    than customer-supplied SOPs."""
    emb = embed_one(f"{title}\n\n{body_markdown}")
    with session_scope() as s:
        fid = _function_id(s, function_slug)
        new_id = s.execute(
            text(
                """
                INSERT INTO sops
                  (function_id, title, body_markdown, source_kind,
                   org_label, industry, confidentiality, embedding, metadata)
                VALUES
                  (:fid, :t, :b, 'seed',
                   :org, :ind, 'public', CAST(:e AS vector), CAST(:m AS JSONB))
                ON CONFLICT (source_kind, title, org_label) DO UPDATE
                  SET body_markdown = EXCLUDED.body_markdown,
                      embedding = EXCLUDED.embedding,
                      function_id = EXCLUDED.function_id,
                      industry = EXCLUDED.industry,
                      updated_at = now()
                RETURNING id
                """
            ),
            {
                "fid": fid, "t": title, "b": body_markdown,
                "org": org_label, "ind": industry,
                "e": _vec_lit(emb),
                "m": json.dumps({"function_slug": function_slug, "seed": True}),
            },
        ).scalar_one()
    return new_id


def store_generated_sop(
    *,
    title: str,
    body_markdown: str,
    function_slug: str | None,
    org_label: str | None,
    industry: str | None,
    consent_to_train: bool,
    created_by: UUID | None = None,
) -> UUID:
    """Persist a freshly generated SOP. If consent_to_train=False we still store
    it (so the user can view/download it) but mark it 'restricted' so retrieval
    excludes it from synthesis seeding for OTHER orgs."""
    emb = embed_one(f"{title}\n\n{body_markdown}")
    confidentiality = "internal" if consent_to_train else "restricted"
    with session_scope() as s:
        fid = _function_id(s, function_slug)
        new_id = s.execute(
            text(
                """
                INSERT INTO sops
                  (function_id, title, body_markdown, source_kind,
                   org_label, industry, confidentiality, embedding, metadata, created_by)
                VALUES
                  (:fid, :t, :b, 'generated',
                   :org, :ind, :cf, CAST(:e AS vector), CAST(:m AS JSONB), :u)
                ON CONFLICT (source_kind, title, org_label) DO UPDATE
                  SET body_markdown = EXCLUDED.body_markdown,
                      embedding = EXCLUDED.embedding,
                      industry = EXCLUDED.industry,
                      confidentiality = EXCLUDED.confidentiality,
                      updated_at = now()
                RETURNING id
                """
            ),
            {
                "fid": fid, "t": title, "b": body_markdown,
                "org": org_label, "ind": industry, "cf": confidentiality,
                "e": _vec_lit(emb),
                "m": json.dumps({
                    "function_slug": function_slug,
                    "consent_to_train": consent_to_train,
                }),
                "u": created_by,
            },
        ).scalar_one()
    return new_id


def list_sops(*, source_kind: str | None = None, function_slug: str | None = None) -> list[SOPRow]:
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT s.id, s.title, f.slug AS function_slug, s.source_kind,
                       s.org_label, s.industry, s.confidentiality, s.body_markdown,
                       s.created_at, s.updated_at
                FROM sops s
                LEFT JOIN functions f ON f.id = s.function_id
                WHERE (:sk IS NULL OR s.source_kind = :sk)
                  AND (:slug IS NULL OR f.slug = :slug)
                ORDER BY s.updated_at DESC
                """
            ),
            {"sk": source_kind, "slug": function_slug},
        ).mappings().all()
        return [SOPRow(**dict(r)) for r in rows]


def get_sop(sop_id: UUID) -> SOPRow | None:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT s.id, s.title, f.slug AS function_slug, s.source_kind,
                       s.org_label, s.industry, s.confidentiality, s.body_markdown,
                       s.created_at, s.updated_at
                FROM sops s
                LEFT JOIN functions f ON f.id = s.function_id
                WHERE s.id = :id
                """
            ),
            {"id": sop_id},
        ).mappings().first()
        return SOPRow(**dict(row)) if row else None


def load_seed_library_from_disk(seed_dir: Path) -> int:
    """Walk a directory of `*.md` files. Frontmatter (YAML-ish first block) is
    optional; we accept simple `key: value` lines before a `---` separator.

    Returns count loaded.
    """
    n = 0
    for md in sorted(seed_dir.glob("*.md")):
        text_doc = md.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text_doc)
        title = meta.get("title") or md.stem.replace("_", " ").title()
        function_slug = meta.get("function") or "operations"
        industry = meta.get("industry")
        insert_seed_sop(
            title=title,
            body_markdown=body.strip(),
            function_slug=function_slug,
            industry=industry,
            org_label=meta.get("org_label") or "_seed",
        )
        n += 1
    return n


def _split_frontmatter(doc: str) -> tuple[dict[str, str], str]:
    if not doc.startswith("---"):
        return {}, doc
    parts = doc.split("---", 2)
    if len(parts) < 3:
        return {}, doc
    meta_block, body = parts[1], parts[2]
    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, body
