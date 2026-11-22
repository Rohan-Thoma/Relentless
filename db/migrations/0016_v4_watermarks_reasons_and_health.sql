-- v4: Source query watermarks, ATS board health/ETag self-pruning,
-- structured rejection/skip reasons with intelligent learning gate,
-- work-mode metadata, and direct hiring contact extraction.

CREATE TABLE IF NOT EXISTS source_watermarks (
    source_key TEXT PRIMARY KEY,
    last_polled_at TIMESTAMPTZ,
    last_etag TEXT,
    last_item_count INTEGER DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE discovered_ats_boards
    ADD COLUMN IF NOT EXISTS etag TEXT,
    ADD COLUMN IF NOT EXISTS consecutive_failures INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_http_status INTEGER,
    ADD COLUMN IF NOT EXISTS matching_jobs_30d INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_poll_at TIMESTAMPTZ;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS skip_reason_category VARCHAR(64),
    ADD COLUMN IF NOT EXISTS skip_reason_note TEXT,
    ADD COLUMN IF NOT EXISTS learn_from_action BOOLEAN,
    ADD COLUMN IF NOT EXISTS hiring_contact_email TEXT,
    ADD COLUMN IF NOT EXISTS work_mode VARCHAR(32),
    ADD COLUMN IF NOT EXISTS external_job_id VARCHAR(256);

CREATE INDEX IF NOT EXISTS idx_jobs_source_external_id
    ON jobs (source_ats, external_job_id)
    WHERE external_job_id IS NOT NULL;
