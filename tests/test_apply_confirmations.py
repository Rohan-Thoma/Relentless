import os
import pytest

from dotenv import load_dotenv
from src.db import get_connection, consume_apply_confirmation, create_apply_confirmation

load_dotenv(override=True)


def _db_available():
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="database unavailable")
def test_invalid_token_rejected():
    assert consume_apply_confirmation("not-a-real-token", job_id=1, kind="SUBMIT") is None


@pytest.mark.skipif(not _db_available(), reason="database unavailable")
def test_confirmation_single_use():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (dedup_hash, source_ats, company, title, location, apply_url) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                ("testhash_" + os.urandom(8).hex(), "greenhouse", "TestCo", "Engineer", "Remote", "https://x"),
            )
            job_id = cur.fetchone()["id"]
        conn.commit()
    try:
        raw, _ = create_apply_confirmation(job_id, kind="SUBMIT", payload={"pdf_sha256": "abc", "apply_url": "https://x"}, ttl_seconds=60)
        first = consume_apply_confirmation(raw, job_id=job_id, kind="SUBMIT")
        assert first is not None
        assert consume_apply_confirmation(raw, job_id=job_id, kind="SUBMIT") is None
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE id = %s;", (job_id,))
            conn.commit()