ALTER TABLE jobs ADD COLUMN IF NOT EXISTS apply_status TEXT DEFAULT 'NOT_APPLIED';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS apply_last_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS apply_events (
    id SERIAL PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL DEFAULT 1,
    router_path TEXT,
    mode TEXT,
    status TEXT,
    error TEXT,
    fields_filled JSONB,
    screenshots TEXT[],
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);