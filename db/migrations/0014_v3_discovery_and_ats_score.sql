ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ats_scorecard JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS original_source_url TEXT;

CREATE TABLE IF NOT EXISTS discovered_ats_boards (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(32) NOT NULL,
    slug VARCHAR(128) NOT NULL,
    company VARCHAR(128),
    metadata JSONB DEFAULT '{}'::jsonb,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    last_polled_at TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE,
    UNIQUE(platform, slug)
);

CREATE TABLE IF NOT EXISTS processed_emails (
    message_id VARCHAR(256) PRIMARY KEY,
    subject TEXT,
    sender TEXT,
    jobs_extracted INTEGER DEFAULT 0,
    processed_at TIMESTAMPTZ DEFAULT NOW()
);

