-- 005 — Procedure-level contradiction findings (Company Brain layer).
-- Mirrors conflict_findings (which is decision-level) for the brain.

CREATE TABLE IF NOT EXISTS procedure_conflict_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    triggering_source_id UUID NOT NULL REFERENCES sources(id),
    conflicting_procedure_id UUID NOT NULL REFERENCES procedures(id),
    new_procedure_id UUID REFERENCES procedures(id),
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    rationale TEXT NOT NULL,
    extraction_id UUID NOT NULL REFERENCES extractions(id),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'dismissed', 'resolved')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_procedure_conflict_findings_status ON procedure_conflict_findings(status);
CREATE INDEX IF NOT EXISTS idx_procedure_conflict_findings_new ON procedure_conflict_findings(new_procedure_id);

-- Extend the edges table to allow procedure-to-procedure relations so the
-- contradicts relation appears in the same graph the engineering layer uses.
DO $$
BEGIN
  ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_src_kind_check;
  ALTER TABLE edges ADD CONSTRAINT edges_src_kind_check
    CHECK (src_kind IN ('decision', 'assumption', 'constraint', 'alternative', 'procedure'));

  ALTER TABLE edges DROP CONSTRAINT IF EXISTS edges_dst_kind_check;
  ALTER TABLE edges ADD CONSTRAINT edges_dst_kind_check
    CHECK (dst_kind IN ('decision', 'assumption', 'constraint', 'alternative', 'procedure'));
END $$;
