"""Async ingest jobs.

Spawns a daemon thread to do GitHub backfill / repo ingest / member refresh,
and writes progress to `ingest_jobs` so the dashboard can show live counts
without us needing a real task queue (Celery/RQ/Arq) for v1.

Demo-cut limits: one job per process, no retries, no cross-process locks.
That's fine for a single-tenant install. v1 swaps this for arq when we have
a second deployment to coordinate against.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import text

from edm.db import session_scope
from edm.logging import get_logger

log = get_logger(__name__)


def create_job(*, kind: str, target: str | None, started_by: UUID | None, total: int = 0) -> UUID:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                INSERT INTO ingest_jobs (kind, target, started_by, total, status)
                VALUES (:k, :t, :u, :tot, 'queued')
                RETURNING id
                """
            ),
            {"k": kind, "t": target, "u": started_by, "tot": total},
        ).mappings().one()
    return row["id"]


def mark_running(job_id: UUID, *, total: int | None = None) -> None:
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE ingest_jobs SET status='running'"
                + (", total=:tot" if total is not None else "")
                + " WHERE id=:id"
            ),
            {"id": job_id, **({"tot": total} if total is not None else {})},
        )


def bump(job_id: UUID, *, processed: int = 0, new_sources: int = 0, new_decisions: int = 0, new_findings: int = 0) -> None:
    with session_scope() as s:
        s.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET processed = processed + :p,
                       new_sources = new_sources + :ns,
                       new_decisions = new_decisions + :nd,
                       new_findings = new_findings + :nf
                 WHERE id = :id
                """
            ),
            {"id": job_id, "p": processed, "ns": new_sources, "nd": new_decisions, "nf": new_findings},
        )


def mark_done(job_id: UUID, *, error: str | None = None) -> None:
    status = "failed" if error else "done"
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE ingest_jobs SET status=:st, error=:err, finished_at=now() WHERE id=:id"
            ),
            {"id": job_id, "st": status, "err": error},
        )


def latest_jobs(limit: int = 10) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT j.id, j.kind, j.target, j.status, j.total, j.processed,
                       j.new_sources, j.new_decisions, j.new_findings, j.error,
                       j.started_at, j.finished_at, u.username
                FROM ingest_jobs j
                LEFT JOIN users u ON u.id = j.started_by
                ORDER BY j.started_at DESC
                LIMIT :l
                """
            ),
            {"l": limit},
        ).mappings().all()
    return [dict(r) for r in rows]


def active_job() -> dict | None:
    with session_scope() as s:
        row = s.execute(
            text(
                """
                SELECT id, kind, target, status, total, processed, started_at
                FROM ingest_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
    return dict(row) if row else None


def get_job(job_id: UUID) -> dict | None:
    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT id, kind, target, status, total, processed, "
                "       new_sources, new_decisions, new_findings, error, "
                "       started_at, finished_at "
                "FROM ingest_jobs WHERE id=:id"
            ),
            {"id": job_id},
        ).mappings().first()
    return dict(row) if row else None


def spawn(target: Callable[[UUID], None], *, kind: str, target_label: str | None, started_by: UUID | None, total: int = 0) -> UUID:
    """Create a job row and start `target(job_id)` on a daemon thread.
    `target` is responsible for calling mark_running / bump / mark_done."""
    job_id = create_job(kind=kind, target=target_label, started_by=started_by, total=total)

    def _run():
        try:
            target(job_id)
            # If target did not mark done, do it now.
            cur = get_job(job_id)
            if cur and cur["status"] == "running":
                mark_done(job_id)
        except Exception as e:
            log.error("job.failed", job_id=str(job_id), error=str(e), tb=traceback.format_exc())
            mark_done(job_id, error=f"{type(e).__name__}: {e}")

    t = threading.Thread(target=_run, name=f"edm-job-{kind}-{job_id}", daemon=True)
    t.start()
    return job_id
