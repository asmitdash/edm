-- 004 — Company Brain + SOP Generator
-- Adds non-engineering knowledge layers on top of the existing decision graph:
--   * functions / departments (sales, support, ops, finance, legal, eng, ...)
--   * procedures (executable how-to units extracted from any source)
--   * procedure steps + guardrails (atomic, agent-runnable)
--   * SOP corpus (seed templates + customer-uploaded + system-generated)
--   * SOP generation sessions (form + chatbot transcripts, status, output ids)
--   * Skills files (executable JSON action graphs paired with each SOP)
--
-- Original engineering decision tables remain untouched. Sources gain new kinds
-- so the same ingestion path can carry SOP-style content.

-- ============================================================
-- Extend `sources.kind` so SOP / brain artifacts ride the same pipeline.
-- ============================================================
DO $$
BEGIN
  ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_kind_check;
  ALTER TABLE sources ADD CONSTRAINT sources_kind_check
    CHECK (kind IN (
      -- engineering (v1)
      'pr', 'commit', 'pr_review', 'pr_comment',
      'slack_message', 'slack_thread',
      'design_doc', 'adr',
      'markdown_upload', 'mermaid_upload', 'svg_upload', 'image_upload',
      -- company-brain (v2)
      'email_thread', 'support_ticket', 'crm_note', 'meeting_transcript',
      'wiki_page', 'policy_doc',
      -- SOP-specific
      'sop_seed', 'sop_uploaded', 'sop_generated',
      'sop_chat_session'
    ));
END $$;

-- ============================================================
-- Functions (departments / business units a procedure belongs to).
-- ============================================================
CREATE TABLE IF NOT EXISTS functions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    description TEXT,
    parent_id UUID REFERENCES functions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO functions (slug, label, description) VALUES
  ('engineering',  'Engineering',           'Software engineering, infra, SRE'),
  ('product',      'Product',               'Product management, design, roadmap'),
  ('sales',        'Sales',                 'Outbound, inbound, account exec, deal desk'),
  ('support',      'Customer Support',      'Help desk, refunds, escalations'),
  ('success',      'Customer Success',      'Onboarding, renewals, churn prevention'),
  ('marketing',    'Marketing',             'Brand, content, growth, lifecycle'),
  ('finance',      'Finance',               'AP / AR, billing, treasury, tax'),
  ('hr',           'People / HR',           'Hiring, onboarding, performance, exits'),
  ('legal',        'Legal',                 'Contracts, compliance, IP, privacy'),
  ('it',           'IT / Workplace',        'Devices, access provisioning, SaaS admin'),
  ('operations',   'Operations',            'Vendors, facilities, logistics'),
  ('security',     'Security',              'AppSec, IR, vuln management')
ON CONFLICT (slug) DO NOTHING;

-- ============================================================
-- Procedures: the central Company-Brain primitive.
-- A procedure is the canonical "how X gets done in this company" unit.
-- It has steps (ordered) and guardrails (constraints / exceptions).
-- ============================================================
CREATE TABLE IF NOT EXISTS procedures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    function_id UUID REFERENCES functions(id),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    trigger_description TEXT,                                  -- "when does this run?"
    owner_role TEXT,                                           -- "support lead", "ops manager"
    extraction_id UUID REFERENCES extractions(id),             -- nullable: hand-authored procedures exist
    sop_id UUID,                                               -- back-link to the SOP this came from (set later)
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'deprecated')),
    embedding vector(1024),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_procedures_function ON procedures(function_id);
CREATE INDEX IF NOT EXISTS idx_procedures_status ON procedures(status);
CREATE INDEX IF NOT EXISTS idx_procedures_embedding
  ON procedures USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS procedure_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    instruction TEXT NOT NULL,
    actor_role TEXT,                                           -- who runs this step
    tool_or_system TEXT,                                       -- "Zendesk", "Stripe dashboard"
    expected_outcome TEXT,
    UNIQUE (procedure_id, step_index)
);

CREATE TABLE IF NOT EXISTS procedure_guardrails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    procedure_id UUID NOT NULL REFERENCES procedures(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('always', 'never', 'condition', 'escalation', 'exception', 'compliance')),
    statement TEXT NOT NULL,
    condition_expr TEXT,                                       -- optional machine-readable predicate
    severity TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN ('low', 'medium', 'high', 'critical'))
);

-- ============================================================
-- SOPs: the human-readable artefact + corpus retrieval surface.
-- A single SOP row tracks its source category, function, and content.
-- generated SOPs link back to a generation_session and a skills_file.
-- ============================================================
CREATE TABLE IF NOT EXISTS sops (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    function_id UUID REFERENCES functions(id),
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('seed', 'uploaded', 'generated')),
    source_id UUID REFERENCES sources(id),                     -- the raw source row, if any
    org_label TEXT,                                            -- "ACME Logistics"
    industry TEXT,                                             -- "logistics", "saas", "healthcare"
    confidentiality TEXT NOT NULL DEFAULT 'internal'
      CHECK (confidentiality IN ('public', 'internal', 'restricted')),
    embedding vector(1024),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_kind, title, org_label)
);
CREATE INDEX IF NOT EXISTS idx_sops_function ON sops(function_id);
CREATE INDEX IF NOT EXISTS idx_sops_source_kind ON sops(source_kind);
CREATE INDEX IF NOT EXISTS idx_sops_embedding
  ON sops USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- back-link procedures.sop_id now that sops exists
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'procedures_sop_id_fkey'
  ) THEN
    ALTER TABLE procedures
      ADD CONSTRAINT procedures_sop_id_fkey
      FOREIGN KEY (sop_id) REFERENCES sops(id) ON DELETE SET NULL;
  END IF;
END $$;

-- ============================================================
-- SOP generation sessions (form + chatbot iterative intake).
-- One session -> one generated SOP + one skills file at the end.
-- ============================================================
CREATE TABLE IF NOT EXISTS sop_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    function_id UUID REFERENCES functions(id),
    title TEXT,                                                -- e.g. "Refund handling SOP"
    org_label TEXT,
    industry TEXT,
    initiated_by UUID REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
      'open', 'collecting_form', 'chatting', 'ready_to_generate',
      'generating', 'completed', 'abandoned'
    )),
    form_data JSONB NOT NULL DEFAULT '{}'::jsonb,              -- canonical structured form fields
    derived_exceptions JSONB NOT NULL DEFAULT '[]'::jsonb,     -- accumulated from chat
    retrieved_sop_ids UUID[] NOT NULL DEFAULT '{}',            -- corpus seed for synthesis
    consent_to_train BOOLEAN NOT NULL DEFAULT true,            -- the disclaimer toggle
    generated_sop_id UUID REFERENCES sops(id),
    generated_skills_file_id UUID,                             -- FK added below
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sop_sessions_status ON sop_sessions(status);
CREATE INDEX IF NOT EXISTS idx_sop_sessions_initiated_by ON sop_sessions(initiated_by);

CREATE TABLE IF NOT EXISTS sop_session_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES sop_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sop_session_messages_session
  ON sop_session_messages(session_id, created_at);

-- ============================================================
-- Skills files: the EXECUTABLE artefact paired with each generated SOP.
-- Stored as a versioned JSON action graph.
-- This is the half of the output that AI agents read; humans read body_markdown.
-- ============================================================
CREATE TABLE IF NOT EXISTS skills_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sop_id UUID NOT NULL REFERENCES sops(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    schema_version TEXT NOT NULL DEFAULT 'skills.v1',
    body JSONB NOT NULL,                                       -- the action graph itself
    checksum TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (sop_id, version)
);
CREATE INDEX IF NOT EXISTS idx_skills_files_sop ON skills_files(sop_id);

-- now that skills_files exists, finish the FK on sop_sessions.generated_skills_file_id
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'sop_sessions_skills_file_fkey'
  ) THEN
    ALTER TABLE sop_sessions
      ADD CONSTRAINT sop_sessions_skills_file_fkey
      FOREIGN KEY (generated_skills_file_id) REFERENCES skills_files(id) ON DELETE SET NULL;
  END IF;
END $$;
