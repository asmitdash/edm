"""GitHub ingestor.

Pulls PRs (with reviews + review comments) from a repo and writes one row per
artifact into `sources`. Idempotent on (kind, external_id).

Demo-cut path: a single repo specified via TARGET_REPO. v1 expansion: per-org
GitHub App install, multi-repo discovery, webhook-driven incremental sync.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from github import Github
from github.PullRequest import PullRequest
from sqlalchemy import text

from edm.db import session_scope
from edm.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SourceRecord:
    kind: str
    external_id: str
    repo: str
    url: str
    title: str | None
    body: str
    author: str | None
    occurred_at: datetime
    metadata: dict


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _records_for_pr(repo_full: str, pr: PullRequest) -> Iterator[SourceRecord]:
    yield SourceRecord(
        kind="pr",
        external_id=f"{repo_full}#{pr.number}",
        repo=repo_full,
        url=pr.html_url,
        title=pr.title,
        body=pr.body or "",
        author=pr.user.login if pr.user else None,
        occurred_at=pr.created_at,
        metadata={
            "number": pr.number,
            "state": pr.state,
            "merged": pr.merged,
            "base": pr.base.ref if pr.base else None,
            "head": pr.head.ref if pr.head else None,
        },
    )

    for review in pr.get_reviews():
        if not review.body:
            continue
        yield SourceRecord(
            kind="pr_review",
            external_id=f"{repo_full}#{pr.number}/review/{review.id}",
            repo=repo_full,
            url=review.html_url,
            title=f"Review on PR #{pr.number} by {review.user.login if review.user else '?'}",
            body=review.body,
            author=review.user.login if review.user else None,
            occurred_at=review.submitted_at or pr.created_at,
            metadata={"pr_number": pr.number, "state": review.state},
        )

    for comment in pr.get_issue_comments():
        if not comment.body:
            continue
        yield SourceRecord(
            kind="pr_comment",
            external_id=f"{repo_full}#{pr.number}/comment/{comment.id}",
            repo=repo_full,
            url=comment.html_url,
            title=f"Comment on PR #{pr.number} by {comment.user.login if comment.user else '?'}",
            body=comment.body,
            author=comment.user.login if comment.user else None,
            occurred_at=comment.created_at,
            metadata={"pr_number": pr.number},
        )


def ingest_repo(token: str, repo_full: str, *, max_prs: int | None = None, state: str = "all") -> int:
    """Ingest PRs from a repo. Returns the number of new source rows written."""
    gh = Github(token)
    repo = gh.get_repo(repo_full)
    log.info("ingest.start", repo=repo_full, state=state, max_prs=max_prs)

    written = 0
    with session_scope() as session:
        for i, pr in enumerate(repo.get_pulls(state=state, sort="created", direction="desc")):
            if max_prs is not None and i >= max_prs:
                break
            for rec in _records_for_pr(repo_full, pr):
                result = session.execute(
                    text(
                        """
                        INSERT INTO sources
                          (kind, external_id, repo, url, title, body, author,
                           occurred_at, content_hash, metadata)
                        VALUES
                          (:kind, :external_id, :repo, :url, :title, :body, :author,
                           :occurred_at, :content_hash, CAST(:metadata AS JSONB))
                        ON CONFLICT (kind, external_id) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "kind": rec.kind,
                        "external_id": rec.external_id,
                        "repo": rec.repo,
                        "url": rec.url,
                        "title": rec.title,
                        "body": rec.body,
                        "author": rec.author,
                        "occurred_at": rec.occurred_at,
                        "content_hash": _hash(rec.body),
                        "metadata": _json(rec.metadata),
                    },
                )
                if result.first() is not None:
                    written += 1
    log.info("ingest.done", repo=repo_full, new_rows=written)
    return written


def _json(d: dict) -> str:
    import json

    return json.dumps(d, default=str)
