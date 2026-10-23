ALTER TABLE jobs ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS embedding_sim FLOAT;
CREATE INDEX IF NOT EXISTS idx_jobs_embedding ON jobs USING hnsw (embedding vector_cosine_ops);