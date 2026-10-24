ALTER TABLE jobs ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_telegram_message_id BIGINT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_edit_notes JSONB DEFAULT '[]'::jsonb;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workflow_stage VARCHAR(32) DEFAULT 'DISCOVERED';

CREATE TABLE IF NOT EXISTS telegram_message_jobs (
    message_id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_unnotified
    ON jobs(triage_passed, human_action, notified_at);

