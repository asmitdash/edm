"""GitHub backfill: pull recent PRs from each target repo, then run the
extraction pipeline on every newly-ingested source.

Composition over reinvention: uses ingest.github.ingest_repo to write rows,
then pipeline.process_unprocessed to extract + detect contradictions."""

from __future__ import annotations

from typing import Callable
from uuid import UUID

from sqlalchemy import text

from edm import jobs
from edm.db import session_scope
from edm.ingest.github import ingest_repo
from edm.ingest.github_oauth import list_org_repos
from edm.ingest.members import refresh_members
from edm.logging import get_logger
from edm.pipeline import process_source

log = get_logger(__name__)


def _new_source_ids_since(before_count: int) -> list[UUID]:
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT s.id FROM sources s
                LEFT JOIN extractions e ON e.source_id = s.id
                WHERE e.id IS NULL
                ORDER BY s.occurred_at ASC
                """
            )
        ).all()
    return [r[0] for r in rows]


def backfill_org_repo(
    *,
    job_id: UUID,
    token: str,
    repo_full: str,
    max_prs: int = 50,
) -> None:
    """Backfill a single repo: ingest + extract every new source, with progress."""
    jobs.mark_running(job_id, total=max_prs)
    n_new = ingest_repo(token, repo_full, max_prs=max_prs, state="all")
    jobs.bump(job_id, new_sources=n_new)
    log.info("backfill.ingested", repo=repo_full, new_sources=n_new)

    pending = _new_source_ids_since(0)
    jobs.mark_running(job_id, total=len(pending))
    for sid in pending:
        try:
            summary = process_source(sid)
            jobs.bump(
                job_id,
                processed=1,
                new_decisions=int(summary.get("decisions") or 0),
                new_findings=len(summary.get("findings") or []),
            )
        except Exception as e:
            log.error("backfill.process_failed", source_id=str(sid), error=str(e))
            jobs.bump(job_id, processed=1)
    jobs.mark_done(job_id)


def backfill_org(
    *,
    job_id: UUID,
    token: str,
    org: str,
    max_repos: int = 5,
    max_prs_per_repo: int = 30,
    refresh_members_first: bool = True,
) -> None:
    """Backfill: refresh members snapshot, then ingest the N most-recently-updated
    repos in the org, then extract + detect contradictions on each new source."""
    jobs.mark_running(job_id, total=0)

    if refresh_members_first:
        try:
            n = refresh_members(token, org)
            log.info("backfill.members", org=org, n=n)
        except Exception as e:
            log.warning("backfill.members_failed", org=org, error=str(e))

    try:
        repos = list_org_repos(token, org, per_page=max_repos)
    except Exception as e:
        jobs.mark_done(job_id, error=f"list_org_repos failed: {e}")
        return

    targets = [r["full_name"] for r in repos[:max_repos]]
    log.info("backfill.start", org=org, repos=targets)
    jobs.mark_running(job_id, total=len(targets) * max_prs_per_repo)

    total_new_sources = 0
    for repo_full in targets:
        try:
            n_new = ingest_repo(token, repo_full, max_prs=max_prs_per_repo, state="all")
            total_new_sources += n_new
            jobs.bump(job_id, new_sources=n_new)
        except Exception as e:
            log.error("backfill.ingest_failed", repo=repo_full, error=str(e))

    pending = _new_source_ids_since(0)
    log.info("backfill.processing", n=len(pending))
    jobs.mark_running(job_id, total=len(pending))
    for sid in pending:
        try:
            summary = process_source(sid)
            jobs.bump(
                job_id,
                processed=1,
                new_decisions=int(summary.get("decisions") or 0),
                new_findings=len(summary.get("findings") or []),
            )
        except Exception as e:
            log.error("backfill.process_failed", source_id=str(sid), error=str(e))
            jobs.bump(job_id, processed=1, error=str(e)[:200])
    jobs.mark_done(job_id)


def spawn_backfill(*, token: str, org: str | None, repo_full: str | None, started_by: UUID | None,
                   max_prs: int = 50) -> UUID:
    """Decide which kind of backfill to run based on what install_config holds."""
    if repo_full:
        return jobs.spawn(
            lambda jid: backfill_org_repo(job_id=jid, token=token, repo_full=repo_full, max_prs=max_prs),
            kind="github_repo_backfill", target_label=repo_full, started_by=started_by, total=max_prs,
        )
    if org:
        return jobs.spawn(
            lambda jid: backfill_org(job_id=jid, token=token, org=org,
                                     max_repos=5, max_prs_per_repo=max_prs // 5 or 10),
            kind="github_org_backfill", target_label=org, started_by=started_by, total=0,
        )
    raise ValueError("backfill requires either repo_full or org")
