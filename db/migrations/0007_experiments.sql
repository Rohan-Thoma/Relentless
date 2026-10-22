CREATE TABLE IF NOT EXISTS experiments (
    id SERIAL PRIMARY KEY,
    proposed_at TIMESTAMPTZ DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    params JSONB,
    status TEXT DEFAULT 'PROPOSED',
    eval_run_id_before INTEGER,
    eval_run_id_after INTEGER
);