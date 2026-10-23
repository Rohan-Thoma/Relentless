-- Phase 2: human-confirmed apply core
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tailored_pdf_path TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tailored_pdf_sha256 VARCHAR(64);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS apply_state TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS apply_feasible BOOLEAN;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS apply_blocker TEXT;

CREATE TABLE IF NOT EXISTS apply_confirmations (
    id SERIAL PRIMARY KEY,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload JSONB,
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    status TEXT DEFAULT 'PENDING'
);

ALTER TABLE apply_events ADD COLUMN IF NOT EXISTS confirmation_id INTEGER;
ALTER TABLE apply_events ADD COLUMN IF NOT EXISTS pdf_sha256 VARCHAR(64);
ALTER TABLE apply_events ADD COLUMN IF NOT EXISTS confirmed_by TEXT;
ALTER TABLE apply_events ADD COLUMN IF NOT EXISTS verification_status TEXT;
ALTER TABLE apply_events ADD COLUMN IF NOT EXISTS evidence_path TEXT;
ALTER TABLE apply_events ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_apply_confirmations_job_status ON apply_confirmations(job_id, status);
CREATE INDEX IF NOT EXISTS idx_apply_events_job_started ON apply_events(job_id, started_at DESC);