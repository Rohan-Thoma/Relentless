CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,
    dedup_hash VARCHAR(64) UNIQUE NOT NULL,
    source_ats VARCHAR(32) NOT NULL,
    company VARCHAR(128) NOT NULL,
    title VARCHAR(256) NOT NULL,
    location VARCHAR(256),
    apply_url TEXT NOT NULL,
    jd_text TEXT,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),

    -- Stage 2: System-1 (Jev) & Embedding Triage Scores
    embedding vector(1536),
    cosine_sim FLOAT,
    jev_scam_prob FLOAT,
    jev_fit_tier VARCHAR(32),
    jev_fit_prob FLOAT,
    jev_persona VARCHAR(64),
    triage_passed BOOLEAN,

    -- Stage 3 & 4: HITL Ground Truth Labels (For ML Eval Harness)
    human_action VARCHAR(32),
    human_action_at TIMESTAMPTZ,
    outcome_status VARCHAR(32) DEFAULT 'PENDING',
    tailored_diff TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_triage ON jobs(triage_passed, human_action);

-- Private Cloud Store for master_profile.json & config.yaml (Keeps PII out of GitHub!)
CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(64) PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);