"""Ingest hand-uploaded sources: markdown, mermaid, svg, png, jpg.

Flow:
  1. Caller passes filename + bytes + uploaded_by user.
  2. We classify by content_type/extension into one of four kinds.
  3. For text kinds (markdown/mermaid/svg) the body IS the file text.
     For images, we call provider.vision_extract once and that text becomes
     the `body` of the source row, with the original bytes attached.
  4. We write a `sources` row + an attachment row, then return the source_id.

The pipeline orchestrator (`pipeline.process_source`) takes it from there.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope
from edm.extract.providers import get_llm_provider
from edm.logging import get_logger

log = get_logger(__name__)

UploadKind = Literal["markdown_upload", "mermaid_upload", "svg_upload", "image_upload"]


_VISION_PROMPT = (
    "You are looking at a software architecture / system-design diagram uploaded by an "
    "engineer. Describe what you see in detail, in prose, focused on engineering decisions: "
    "what components exist, what depends on what, what data flows where, and any explicit "
    "decisions, assumptions, or constraints labeled or implied. Do not invent components "
    "that are not visible. If the image is not an architecture diagram, say so plainly. "
    "Output plain text — no JSON, no markdown headers."
)


def _classify(filename: str, content_type: str | None) -> UploadKind:
    name = filename.lower()
    ct = (content_type or "").lower()
    if name.endswith(".md") or ct == "text/markdown":
        return "markdown_upload"
    if name.endswith(".mmd") or "mermaid" in ct:
        return "mermaid_upload"
    if name.endswith(".svg") or ct == "image/svg+xml":
        return "svg_upload"
    if ct.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg")):
        return "image_upload"
    # Default: treat unknown text-ish files as markdown-shaped.
    return "markdown_upload"


def ingest_upload(
    *,
    filename: str,
    content_type: str | None,
    raw_bytes: bytes,
    uploaded_by: UUID,
    title: str | None = None,
) -> tuple[UUID, UploadKind]:
    kind = _classify(filename, content_type)

    if kind in ("markdown_upload", "mermaid_upload", "svg_upload"):
        body = raw_bytes.decode("utf-8", errors="replace")
        meta = {"filename": filename, "uploaded_by": str(uploaded_by)}
    else:
        # Image: call vision LLM once, store extracted text as the body.
        provider = get_llm_provider()
        result = provider.vision_extract(
            image_bytes=raw_bytes,
            mime_type=content_type or "image/png",
            prompt=_VISION_PROMPT,
        )
        body = result.payload.get("text") or ""
        meta = {
            "filename": filename,
            "uploaded_by": str(uploaded_by),
            "vision_model": result.model,
            "vision_input_tokens": result.input_tokens,
            "vision_output_tokens": result.output_tokens,
        }

    occurred_at = datetime.now(timezone.utc)
    external_id = f"upload/{hashlib.sha256(raw_bytes).hexdigest()[:16]}/{filename}"
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()

    with session_scope() as s:
        row = s.execute(
            text(
                """
                INSERT INTO sources
                  (kind, external_id, repo, url, title, body, author,
                   occurred_at, content_hash, metadata)
                VALUES
                  (:k, :ext, NULL, NULL, :title, :body, NULL,
                   :ts, :h, CAST(:meta AS JSONB))
                ON CONFLICT (kind, external_id) DO UPDATE
                  SET title = EXCLUDED.title,
                      body = EXCLUDED.body,
                      content_hash = EXCLUDED.content_hash,
                      metadata = EXCLUDED.metadata
                RETURNING id
                """
            ),
            {
                "k": kind,
                "ext": external_id,
                "title": title or filename,
                "body": body,
                "ts": occurred_at,
                "h": h,
                "meta": json.dumps(meta),
            },
        ).mappings().one()
        source_id: UUID = row["id"]

        s.execute(
            text(
                """
                INSERT INTO source_attachments
                  (source_id, filename, content_type, bytes, uploaded_by)
                VALUES (:sid, :fn, :ct, :b, :u)
                """
            ),
            {
                "sid": source_id,
                "fn": filename,
                "ct": content_type or "application/octet-stream",
                "b": raw_bytes,
                "u": uploaded_by,
            },
        )

    log.info("upload.ingested", kind=kind, filename=filename, source_id=str(source_id), bytes=len(raw_bytes))
    return source_id, kind
