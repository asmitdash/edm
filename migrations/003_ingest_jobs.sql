-- 003 — async ingest job tracking so the dashboard can show live progress.
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL,                                                -- 'github_backfill', 'github_repo_backfill', etc.
    target TEXT,                                                       -- e.g. 'Deloitte-Data-Automation' or 'org/repo'
    status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','cancelled')) DEFAULT 'queued',
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    new_sources INTEGER NOT NULL DEFAULT 0,
    new_decisions INTEGER NOT NULL DEFAULT 0,
    new_findings INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_by UUID REFERENCES users(id),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_started ON ingest_jobs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_jobs_status ON ingest_jobs(status);
