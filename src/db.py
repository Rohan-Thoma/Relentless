import os
import hashlib
import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

SCHEMA_SQL = """
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
    jev_scam_prob FLOAT,          -- noul: probability of ghost/scam/AI-harvester
    jev_fit_tier VARCHAR(32),     -- score: reject | weak | moderate | strong | dream
    jev_fit_prob FLOAT,           -- calibrated probability of strong+ fit
    jev_persona VARCHAR(64),      -- choice: selected base resume variant
    triage_passed BOOLEAN,

    -- Stage 3 & 4: HITL Ground Truth Labels (For ML Eval Harness)
    human_action VARCHAR(32),     -- APPLIED | SKIP_BAD_FIT | SKIP_SCAM | EDITED
    human_action_at TIMESTAMPTZ,
    outcome_status VARCHAR(32) DEFAULT 'PENDING', -- PENDING | CALLBACK | INTERVIEW | REJECTED
    tailored_diff TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_triage ON jobs(triage_passed, human_action);
"""

def get_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is missing in .env")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def make_dedup_hash(company: str, title: str, location: str) -> str:
    """Creates a deterministic SHA-256 hash so we never process the same job twice."""
    norm = f"{company.strip().lower()}|{title.strip().lower()}|{location.strip().lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    print("✅ Neon PostgreSQL initialized with pgvector and Evaluation Harness schema!")

if __name__ == "__main__":
    init_db()