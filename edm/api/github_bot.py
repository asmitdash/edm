"""GitHub PR-review bot. Wired to the /webhooks/github endpoint.

Flow on `pull_request.opened|edited|synchronize|ready_for_review`:
  1. Upsert the PR as a source row.
  2. Run the pipeline (extract decisions, detect contradictions).
  3. For each contradiction finding, post a single PR review comment summarizing
     the conflict and linking to the prior decision in the graph.

Auth: uses a personal access token in the demo-cut path (GITHUB_TOKEN env var).
v1 swaps this for GitHub App installation-token issuance.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from edm.config import get_settings
from edm.db import session_scope
from edm.logging import get_logger
from edm.pipeline import process_source

log = get_logger(__name__)


async def handle_pull_request_event(payload: dict[str, Any]) -> dict[str, Any]:
    pr = payload["pull_request"]
    repo = payload["repository"]["full_name"]
    number = pr["number"]
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    author = (pr.get("user") or {}).get("login")
    occurred_at = pr["created_at"]
    url = pr["html_url"]

    # 1. upsert source
    with session_scope() as session:
        row = session.execute(
            text(
                """
                INSERT INTO sources
                  (kind, external_id, repo, url, title, body, author,
                   occurred_at, content_hash, metadata)
                VALUES
                  ('pr', :external_id, :repo, :url, :title, :body, :author,
                   :occurred_at, :hash, CAST(:meta AS JSONB))
                ON CONFLICT (kind, external_id) DO UPDATE
                  SET title = EXCLUDED.title,
                      body = EXCLUDED.body,
                      content_hash = EXCLUDED.content_hash,
                      metadata = EXCLUDED.metadata
                RETURNING id
                """
            ),
            {
                "external_id": f"{repo}#{number}",
                "repo": repo,
                "url": url,
                "title": title,
                "body": body,
                "author": author,
                "occurred_at": occurred_at,
                "hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "meta": json.dumps({"number": number, "state": pr.get("state")}),
            },
        ).mappings().first()
        source_id: UUID = row["id"]

    # 2. run pipeline (extraction + reasoning)
    summary = process_source(source_id)

    # 3. for each finding, post a PR comment
    posted: list[str] = []
    for finding_id in summary.get("findings", []):
        url_posted = await _post_finding_comment(repo, number, finding_id)
        if url_posted:
            posted.append(url_posted)

    summary["posted_comments"] = posted
    log.info("pr_bot.done", repo=repo, number=number, posted=len(posted))
    return summary


async def _post_finding_comment(repo: str, pr_number: int, finding_id: str) -> str | None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("pr_bot.no_token")
        return None

    with session_scope() as session:
        row = session.execute(
            text(
                """
                SELECT cf.severity, cf.rationale,
                       d.title AS prior_title, d.summary AS prior_summary,
                       sp.url AS prior_source_url
                FROM conflict_findings cf
                JOIN decisions d ON d.id = cf.conflicting_decision_id
                JOIN extractions e ON e.id = d.extraction_id
                JOIN sources sp ON sp.id = e.source_id
                WHERE cf.id = :id
                """
            ),
            {"id": finding_id},
        ).mappings().first()
        if row is None:
            return None

    settings = get_settings()
    base_url = f"http://{settings.host}:{settings.port}"
    body = (
        f"### EDM — possible conflict with prior decision\n\n"
        f"**Severity:** {row['severity']}\n\n"
        f"This PR appears to contradict an earlier decision: **{row['prior_title']}**\n\n"
        f"> {row['prior_summary']}\n\n"
        f"**Why this looks like a conflict:** {row['rationale']}\n\n"
        f"Sources: [prior decision]({row['prior_source_url']}) · [graph view]({base_url}/graph)"
    )

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"body": body},
        )
    if resp.status_code >= 300:
        log.error("pr_bot.comment_failed", status=resp.status_code, body=resp.text[:500])
        return None
    posted_url = resp.json().get("html_url")

    with session_scope() as session:
        session.execute(
            text("UPDATE conflict_findings SET posted_pr_comment_url = :u WHERE id = :id"),
            {"u": posted_url, "id": finding_id},
        )

    return posted_url
