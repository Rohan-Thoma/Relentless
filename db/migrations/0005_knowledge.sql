CREATE TABLE IF NOT EXISTS okf_concepts (
    path TEXT PRIMARY KEY,
    concept_type TEXT NOT NULL,
    name TEXT,
    description TEXT,
    resource TEXT,
    tags TEXT[],
    frontmatter JSONB NOT NULL DEFAULT '{}'::jsonb,
    verified JSONB DEFAULT '[]'::jsonb,
    status TEXT DEFAULT 'stable',
    stale_after TIMESTAMPTZ,
    body TEXT NOT NULL,
    body_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_okf_tags ON okf_concepts USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_okf_frontmatter ON okf_concepts USING GIN (frontmatter);
CREATE INDEX IF NOT EXISTS idx_okf_body_tsv ON okf_concepts USING GIN (body_tsv);

CREATE TABLE IF NOT EXISTS knowledge_revisions (
    id SERIAL PRIMARY KEY,
    path TEXT NOT NULL REFERENCES okf_concepts(path) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    frontmatter JSONB,
    body TEXT,
    actor TEXT,
    at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id SERIAL PRIMARY KEY,
    ran_at TIMESTAMPTZ DEFAULT NOW(),
    n INTEGER,
    metrics JSONB,
    config_version TEXT
);

CREATE TABLE IF NOT EXISTS pending_actions (
    token TEXT PRIMARY KEY,
    kind TEXT,
    payload JSONB,
    expires_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS knowledge_forget (
    pattern TEXT PRIMARY KEY,
    forgotten_at TIMESTAMPTZ DEFAULT NOW()
);