-- Engineering Decision Memory — initial schema
-- Design intent: every claim in the graph traces back to a source artifact via an extraction.
-- Provenance is a JOIN, not a feature.

-- Extensions are expected to already be installed by the DB superuser:
--   CREATE EXTENSION IF NOT EXISTS pgcrypto;
--   CREATE EXTENSION IF NOT EXISTS vector;
-- We do not run them here so this migration can be applied by a non-superuser
-- application role.

-- ============================================================
-- Sources: every artifact ingested. Raw, immutable, content-addressable.
-- ============================================================
CREATE TABLE sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN ('pr', 'commit', 'pr_review', 'pr_comment', 'slack_message', 'slack_thread', 'design_doc', 'adr')),
    external_id TEXT NOT NULL,
    repo TEXT,
    url TEXT,
    title TEXT,
    body TEXT NOT NULL,
    author TEXT,
    permissions JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash TEXT NOT NULL,
    UNIQUE (kind, external_id)
);
CREATE INDEX idx_sources_kind_occurred ON sources(kind, occurred_at DESC);
CREATE INDEX idx_sources_repo ON sources(repo);

-- ============================================================
-- Source chunks: retrieval unit. Each chunk has its own embedding.
-- ============================================================
CREATE TABLE source_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding vector(1024),
    UNIQUE (source_id, chunk_index)
);
CREATE INDEX idx_source_chunks_source ON source_chunks(source_id);
CREATE INDEX idx_source_chunks_embedding ON source_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Extractions: every LLM call that produced graph data. Audit trail.
-- ============================================================
CREATE TABLE extractions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    raw_response JSONB NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_extractions_source ON extractions(source_id);

-- ============================================================
-- Owners: people. Decoupled from decisions so attribution survives org changes.
-- ============================================================
CREATE TABLE owners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    handle TEXT NOT NULL UNIQUE,
    display_name TEXT,
    email TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ============================================================
-- Decisions: the central node type.
-- ============================================================
CREATE TABLE decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'reverted', 'stale')),
    decided_at TIMESTAMPTZ,
    embedding vector(1024),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_decisions_status ON decisions(status);
CREATE INDEX idx_decisions_embedding ON decisions USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Assumptions: what a decision relied on being true.
-- ============================================================
CREATE TABLE assumptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    statement TEXT NOT NULL,
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invalidated', 'unknown')),
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_assumptions_status ON assumptions(status);
CREATE INDEX idx_assumptions_embedding ON assumptions USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ============================================================
-- Constraints: bounds the decision had to respect (latency, compliance, budget...).
-- ============================================================
CREATE TABLE constraints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    statement TEXT NOT NULL,
    kind TEXT NOT NULL,
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'lifted', 'unknown')),
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_constraints_status ON constraints(status);

-- ============================================================
-- Decision alternatives: other options considered and rejected.
-- ============================================================
CREATE TABLE decision_alternatives (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    rejection_reason TEXT,
    extraction_id UUID NOT NULL REFERENCES extractions(id)
);

-- ============================================================
-- Decision ownership: many-to-many; preserves history when owners leave.
-- ============================================================
CREATE TABLE decision_owners (
    decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL REFERENCES owners(id),
    role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'reviewer', 'stakeholder')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (decision_id, owner_id, role)
);

-- ============================================================
-- Edges: typed, polymorphic, confidence-scored, with provenance.
-- A claim that two graph nodes relate.
-- ============================================================
CREATE TABLE edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    src_kind TEXT NOT NULL CHECK (src_kind IN ('decision', 'assumption', 'constraint', 'alternative')),
    src_id UUID NOT NULL,
    dst_kind TEXT NOT NULL CHECK (dst_kind IN ('decision', 'assumption', 'constraint', 'alternative')),
    dst_id UUID NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN (
        'depends_on',       -- decision depends on assumption / constraint
        'motivated_by',     -- decision motivated by constraint
        'supersedes',       -- decision supersedes another decision
        'contradicts',      -- decision contradicts another decision (the headline)
        'related_to',       -- weaker semantic link
        'considered'        -- decision considered an alternative
    )),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_kind, src_id, dst_kind, dst_id, relation)
);
CREATE INDEX idx_edges_src ON edges(src_kind, src_id);
CREATE INDEX idx_edges_dst ON edges(dst_kind, dst_id);
CREATE INDEX idx_edges_relation ON edges(relation);

-- ============================================================
-- Conflict findings: contradictions surfaced by the reasoner.
-- These are the "PR conflicts with prior decision" demo moments.
-- ============================================================
CREATE TABLE conflict_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    triggering_source_id UUID NOT NULL REFERENCES sources(id),    -- e.g. the new PR
    conflicting_decision_id UUID NOT NULL REFERENCES decisions(id),
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    rationale TEXT NOT NULL,
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    posted_pr_comment_url TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'dismissed', 'resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conflict_findings_status ON conflict_findings(status);

-- ============================================================
-- Stale assumption findings: the second headline demo moment.
-- ============================================================
CREATE TABLE stale_assumption_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assumption_id UUID NOT NULL REFERENCES assumptions(id),
    triggering_source_id UUID REFERENCES sources(id),
    rationale TEXT NOT NULL,
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'dismissed', 'resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
