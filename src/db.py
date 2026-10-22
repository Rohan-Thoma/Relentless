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
"""

def get_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is missing in .env")
    return psycopg.connect(db_url, row_factory=dict_row)

def make_dedup_hash(company: str, title: str, location: str) -> str:
    norm = f"{company.strip().lower()}|{title.strip().lower()}|{location.strip().lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()

def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    print("✅ Neon PostgreSQL initialized (jobs + private app_settings tables ready)!")

def reset_database(mode: str = "labels") -> str:
    """
    Granular Reset Engine for experimentation vs. live production:
    - 'labels': Soft reset. Keeps scraped jobs & Jev scores, wipes only human button clicks & diffs.
    - 'triage': Medium reset. Keeps scraped jobs, wipes Jev scores + human clicks so you can re-triage.
    - 'all': Hard factory reset. Wipes all jobs and resets ID counter to 1.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            if mode == "labels":
                cur.execute(
                    """
                    UPDATE jobs
                    SET human_action = NULL,
                        human_action_at = NULL,
                        outcome_status = 'PENDING',
                        tailored_diff = NULL;
                    """
                )
                msg = f"🔄 Soft Reset complete: Cleared human labels on {cur.rowcount} jobs (Jev scores preserved, Eval Harness reset to n=0)."
            elif mode == "triage":
                cur.execute(
                    """
                    UPDATE jobs
                    SET cosine_sim = NULL,
                        jev_scam_prob = NULL,
                        jev_fit_tier = NULL,
                        jev_fit_prob = NULL,
                        jev_persona = NULL,
                        triage_passed = NULL,
                        human_action = NULL,
                        human_action_at = NULL,
                        outcome_status = 'PENDING',
                        tailored_diff = NULL;
                    """
                )
                msg = f"🔄 Triage Reset complete: Reset Jev scores & labels on {cur.rowcount} scraped jobs."
            elif mode == "all":
                cur.execute("TRUNCATE TABLE jobs RESTART IDENTITY;")
                msg = "💥 Factory Reset complete: All jobs wiped and ID counter reset to #1."
            else:
                raise ValueError("Invalid reset mode. Choose 'labels', 'triage', or 'all'.")
        conn.commit()
    print(msg)
    return msg

if __name__ == "__main__":
    init_db()