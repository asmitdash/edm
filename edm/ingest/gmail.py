"""Gmail ingestion: pull recent threads, write each as an `email_thread` source.

Uses the Gmail REST API directly via httpx so we don't take on the heavy
google-api-python-client dependency. Supports a search query (`q`) so users
can scope ingestion to a label or sender.

Token refresh is handled here: if the stored access_token is stale we refresh
it via the stored refresh_token and persist the new access_token.

Each email thread becomes ONE `sources` row of kind `email_thread`. The body
is the concatenation of the included messages (decoded, plain-text-preferred)
so the brain extractor sees the conversation as a single procedure source.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from edm import runtime_config
from edm.db import session_scope
from edm.ingest import gmail_oauth
from edm.logging import get_logger

log = get_logger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


@dataclass
class IngestSummary:
    fetched: int
    written: int
    skipped_existing: int
    errors: list[str]


def _stored_token_or_refresh() -> str:
    cfg = runtime_config.load()
    if not cfg.get("gmail_access_token"):
        raise RuntimeError("Gmail not connected. Visit /setup/gmail.")
    # Optimistic: try the stored access token first; refresh if a request 401s.
    return cfg["gmail_access_token"]


def _refresh_and_persist() -> str:
    cfg = runtime_config.load()
    if not cfg.get("gmail_refresh_token"):
        raise RuntimeError("No refresh token; reconnect Gmail at /setup/gmail.")
    tok = gmail_oauth.refresh_access_token(
        client_id=cfg["gmail_client_id"],
        client_secret=cfg["gmail_client_secret"],
        refresh_token=cfg["gmail_refresh_token"],
    )
    runtime_config.update(gmail_access_token=tok.access_token)
    return tok.access_token


def _api_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _stored_token_or_refresh()
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(f"{GMAIL_API}{path}", headers=headers, params=params or {}, timeout=20)
    if r.status_code == 401:
        token = _refresh_and_persist()
        headers["Authorization"] = f"Bearer {token}"
        r = httpx.get(f"{GMAIL_API}{path}", headers=headers, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def _list_threads(*, q: str | None, max_results: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    while len(out) < max_results:
        params: dict[str, Any] = {"maxResults": min(50, max_results - len(out))}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token
        j = _api_get("/users/me/threads", params=params)
        out.extend(j.get("threads", []) or [])
        page_token = j.get("nextPageToken")
        if not page_token:
            break
    return out[:max_results]


def _get_thread(thread_id: str) -> dict[str, Any]:
    return _api_get(f"/users/me/threads/{thread_id}", params={"format": "full"})


def _decode_body(part: dict[str, Any]) -> str:
    body = part.get("body", {}) or {}
    data = body.get("data")
    if not data:
        # multi-part: walk children, prefer text/plain
        parts = part.get("parts") or []
        plain = [p for p in parts if (p.get("mimeType") or "").lower() == "text/plain"]
        chosen = plain or parts
        return "\n\n".join(_decode_body(p) for p in chosen)
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    except Exception:
        return ""


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _flatten_message(msg: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, from, body_text) for a single Gmail message."""
    headers = {h["name"].lower(): h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    subj = headers.get("subject", "")
    sender = headers.get("from", "")
    body = _decode_body(msg.get("payload") or {})
    if "<html" in body.lower() or "<div" in body.lower():
        body = _HTML_TAG_RE.sub("", body)
    return subj, sender, body.strip()


def ingest_recent(*, q: str | None = None, max_threads: int = 25, run_pipeline: bool = True) -> IngestSummary:
    """Pull up to `max_threads` recent Gmail threads, persist each as a source,
    and (optionally) run the brain pipeline on each."""
    summary = IngestSummary(fetched=0, written=0, skipped_existing=0, errors=[])
    try:
        threads = _list_threads(q=q, max_results=max_threads)
    except Exception as e:
        summary.errors.append(f"list threads: {e}")
        return summary

    summary.fetched = len(threads)
    written_ids: list[UUID] = []

    for stub in threads:
        tid = stub["id"]
        try:
            full = _get_thread(tid)
        except Exception as e:
            summary.errors.append(f"thread {tid}: {e}")
            continue

        msgs = full.get("messages") or []
        if not msgs:
            continue

        first_subj, first_from, _ = _flatten_message(msgs[0])
        body_parts = []
        for m in msgs:
            subj, sender, b = _flatten_message(m)
            body_parts.append(f"From: {sender}\nSubject: {subj}\n\n{b}")
        body = "\n\n---\n\n".join(body_parts)
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        external_id = f"gmail/{tid}"
        title = first_subj or "(no subject)"
        author = first_from or None
        ts_ms = int(msgs[0].get("internalDate") or 0)
        occurred_at = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)

        try:
            with session_scope() as s:
                row = s.execute(
                    text(
                        """
                        SELECT id FROM sources WHERE kind='email_thread' AND external_id=:ext
                        """
                    ),
                    {"ext": external_id},
                ).first()
                if row is not None:
                    summary.skipped_existing += 1
                    continue
                source_id = s.execute(
                    text(
                        """
                        INSERT INTO sources
                          (kind, external_id, title, body, author, occurred_at, content_hash, metadata)
                        VALUES
                          ('email_thread', :ext, :title, :body, :author, :ts, :h, CAST(:m AS JSONB))
                        RETURNING id
                        """
                    ),
                    {
                        "ext": external_id, "title": title, "body": body,
                        "author": author, "ts": occurred_at, "h": body_hash,
                        "m": json.dumps({"gmail_thread_id": tid, "n_messages": len(msgs)}),
                    },
                ).scalar_one()
                written_ids.append(source_id)
                summary.written += 1
        except Exception as e:
            summary.errors.append(f"persist {tid}: {e}")

    if run_pipeline and written_ids:
        from edm.pipeline import process_source
        for sid in written_ids:
            try:
                process_source(sid)
            except Exception as e:
                summary.errors.append(f"pipeline {sid}: {e}")

    log.info(
        "gmail.ingest_complete",
        fetched=summary.fetched, written=summary.written,
        skipped=summary.skipped_existing, errors=len(summary.errors),
    )
    return summary
