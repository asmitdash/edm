-- 002 — Multi-user roles, install-level config, audit log, GitHub members.
-- Backwards-compatible: existing tables (sources, decisions, etc.) untouched.

-- ============================================================
-- Users + roles
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'senior', 'junior')),
    active BOOLEAN NOT NULL DEFAULT true,
    must_change_password BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
-- Enforce exactly one active admin via partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_active_admin
  ON users ((TRUE)) WHERE role = 'admin' AND active = true;

-- ============================================================
-- Invite tokens (for senior/admin to invite junior or senior)
-- ============================================================
CREATE TABLE IF NOT EXISTS invites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('senior', 'junior')),
    created_by UUID NOT NULL REFERENCES users(id),
    redeemed_by UUID REFERENCES users(id),
    redeemed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_invites_token ON invites(token);

-- ============================================================
-- Install-level GitHub link (single row).
-- access_token is Fernet-encrypted using a key derived from EDM_SESSION_SECRET.
-- ============================================================
CREATE TABLE IF NOT EXISTS github_install (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    github_login TEXT,
    org_login TEXT,
    repo_full TEXT,
    access_token_enc TEXT,
    scope TEXT,
    connected_by UUID REFERENCES users(id),
    connected_at TIMESTAMPTZ,
    last_validated_at TIMESTAMPTZ,
    last_validation_status TEXT
);
INSERT INTO github_install (id) VALUES (1) ON CONFLICT DO NOTHING;

-- ============================================================
-- Install-level LLM config (single row). Same encryption story.
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider TEXT CHECK (provider IN ('openrouter', 'openai', 'gemini', 'anthropic')),
    model TEXT,
    api_key_enc TEXT,
    configured_by UUID REFERENCES users(id),
    configured_at TIMESTAMPTZ,
    last_validated_at TIMESTAMPTZ,
    last_validation_status TEXT
);
INSERT INTO llm_config (id) VALUES (1) ON CONFLICT DO NOTHING;

-- ============================================================
-- Audit log — admin-visible only. Append-only.
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    action TEXT NOT NULL,
    target TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);

-- ============================================================
-- GitHub org members snapshot (refreshed on demand by seniors/admin).
-- This is the data the PDF report uses for the "members" section.
-- ============================================================
CREATE TABLE IF NOT EXISTS github_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_login TEXT NOT NULL,
    github_login TEXT NOT NULL,
    display_name TEXT,
    avatar_url TEXT,
    role_in_org TEXT,                -- 'member' | 'admin' (their role IN GitHub, not in EDM)
    profile_url TEXT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_login, github_login)
);
CREATE INDEX IF NOT EXISTS idx_github_members_org ON github_members(org_login);

-- ============================================================
-- Source attachments (uploaded files: markdown, mermaid, svg, png, jpg).
-- Binary content stored in `bytes`; vision-LLM extracted text becomes a
-- `sources` row that points back here.
-- ============================================================
CREATE TABLE IF NOT EXISTS source_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    bytes BYTEA NOT NULL,
    uploaded_by UUID REFERENCES users(id),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_source_attachments_source ON source_attachments(source_id);

-- ============================================================
-- Extend `sources` to allow uploaded artefact kinds.
-- Use a helper function so this is idempotent across re-runs.
-- ============================================================
DO $$
BEGIN
  -- Drop the existing CHECK if it forbids the new kinds, then re-add a permissive one.
  ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_kind_check;
  ALTER TABLE sources ADD CONSTRAINT sources_kind_check
    CHECK (kind IN (
      'pr', 'commit', 'pr_review', 'pr_comment',
      'slack_message', 'slack_thread',
      'design_doc', 'adr',
      'markdown_upload', 'mermaid_upload', 'svg_upload', 'image_upload'
    ));
END $$;
