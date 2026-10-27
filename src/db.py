from contextlib import contextmanager
import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

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

_POOL = None


def _dsn():
    return os.getenv("DATABASE_URL_POOLER") or os.getenv("DATABASE_URL")


def get_pool():
    global _POOL
    if _POOL is None:
        if not _dsn():
            raise ValueError("DATABASE_URL is missing in .env")
        _POOL = ConnectionPool(
            conninfo=_dsn(),
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
            },
            check=ConnectionPool.check_connection,
            min_size=1,
            max_size=20,
            max_idle=120.0,
            max_lifetime=1800.0,
            timeout=30.0,
            open=False,
        )
        _POOL.open()
    return _POOL


@contextmanager
def get_connection():
    if not _dsn():
        raise ValueError("DATABASE_URL is missing in .env")
    pool = get_pool()
    for attempt in range(2):
        try:
            with pool.connection() as conn:
                yield conn
                return
        except (psycopg.OperationalError, psycopg.errors.ConnectionException) as e:
            if attempt == 0:
                logger.warning("Database connection dropped (%s). Retrying with fresh connection...", e)
                try:
                    pool.check()
                except Exception:
                    pass
                continue
            raise


def close_pool():
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


BOT_ADVISORY_LOCK_ID = 849201743


def try_acquire_advisory_lock(lock_id: int = BOT_ADVISORY_LOCK_ID):
    """Returns an open autocommit connection holding the session lock, or None if held elsewhere."""
    dsn = _dsn()
    if not dsn:
        return None
    try:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS locked;", (lock_id,))
            row = cur.fetchone()
            locked = bool(row[0] if isinstance(row, tuple) else row["locked"])
        if locked:
            return conn
        conn.close()
    except Exception:
        pass
    return None


def make_dedup_hash(company: str, title: str, location: str) -> str:
    norm = f"{company.strip().lower()}|{title.strip().lower()}|{location.strip().lower()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def init_db():
    from src.db_migrate import run_migrations
    run_migrations()
    print("✅ Neon PostgreSQL initialized (migrations applied)!")


def reset_database(mode: str = "labels") -> str:
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
                cur.execute(
                    "TRUNCATE TABLE jobs, apply_events, apply_confirmations, telegram_message_jobs, outcome_events RESTART IDENTITY CASCADE;"
                )
                try:
                    from src.ingestors.dedup_cache import CACHE
                    CACHE.reset()
                except Exception:
                    pass
                msg = "💥 Factory Reset complete: All jobs wiped and ID counter reset to #1."
            else:
                raise ValueError("Invalid reset mode. Choose 'labels', 'triage', or 'all'.")
        conn.commit()
    print(msg)
    return msg


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_pending_action(kind: str, payload=None, ttl_seconds: int = 600, created_by=None):
    """Create a single-use, expiring pending action. Returns (raw_token, expires_at)."""
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pending_actions (token, kind, payload, expires_at) VALUES (%s, %s, %s, %s);",
                (token_hash, kind, Jsonb(payload or {}), expires_at),
            )
        conn.commit()
    return raw, expires_at


def get_pending_action(raw_token: str):
    """Return the pending action row if it exists and is not expired; does not consume."""
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pending_actions WHERE token = %s AND expires_at > NOW();",
                (token_hash,),
            )
            return cur.fetchone()


def consume_pending_action(raw_token: str, kind: str | None = None):
    """Atomically fetch and delete a pending action if valid and not expired. Returns row or None."""
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM pending_actions
                WHERE token = %s AND expires_at > NOW()
                RETURNING token, kind, payload, expires_at;
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if kind is not None and row["kind"] != kind:
                return None
            conn.commit()
            return row


def create_apply_confirmation(job_id: int, kind: str = "SUBMIT", payload: dict | None = None, ttl_seconds: int = 300):
    raw = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw)
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_confirmations (token_hash, job_id, kind, payload, expires_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (token_hash, job_id, kind, Jsonb(payload or {}), expires_at),
            )
        conn.commit()
    return raw, expires_at


def consume_apply_confirmation(raw_token: str, job_id: int | None = None, kind: str | None = None):
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_id, kind, payload, expires_at, status
                FROM apply_confirmations
                WHERE token_hash = %s AND status = 'PENDING'
                FOR UPDATE;
                """,
                (token_hash,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if kind is not None and row["kind"] != kind:
                return None
            if job_id is not None and row["job_id"] != job_id:
                return None
            expires = row["expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires < datetime.now(UTC):
                cur.execute("UPDATE apply_confirmations SET status='EXPIRED' WHERE id=%s;", (row["id"],))
                conn.commit()
                return None
            cur.execute(
                "UPDATE apply_confirmations SET status='USED', used_at=NOW() WHERE id=%s;",
                (row["id"],),
            )
            conn.commit()
    return row


def delete_pending_action(raw_token: str):
    token_hash = _hash_token(raw_token)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_actions WHERE token = %s;", (token_hash,))
        conn.commit()


if __name__ == "__main__":
    init_db()
