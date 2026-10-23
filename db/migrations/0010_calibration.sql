CREATE TABLE IF NOT EXISTS calibration_models (
    id SERIAL PRIMARY KEY,
    fitted_at TIMESTAMPTZ DEFAULT NOW(),
    n INTEGER,
    method TEXT,
    mapping JSONB,
    metrics JSONB,
    active BOOLEAN DEFAULT FALSE,
    created_by TEXT
);