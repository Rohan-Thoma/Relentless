ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS tailored_resume_json JSONB,
    ADD COLUMN IF NOT EXISTS active_chat_id BIGINT;

ALTER TABLE telegram_message_jobs
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_telegram_message_jobs_active
    ON telegram_message_jobs (job_id, chat_id, is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_jobs_company_lower
    ON jobs (LOWER(company));

CREATE INDEX IF NOT EXISTS idx_jobs_pending_triage
    ON jobs (id)
    WHERE triage_passed IS NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_recoverable_stages
    ON jobs (workflow_stage)
    WHERE workflow_stage IN ('PROCESSING', 'EDITING', 'AWAITING_EDIT_NOTE', 'SUBMITTING');

