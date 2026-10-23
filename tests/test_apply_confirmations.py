import pytest

from dotenv import load_dotenv

load_dotenv(override=True)

from src.db import consume_apply_confirmation, create_apply_confirmation


def _db_available():
    try:
        from src.db import get_connection

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
    raw, _ = create_apply_confirmation(4, kind="SUBMIT", payload={"pdf_sha256": "abc", "apply_url": "https://x"}, ttl_seconds=60)
    first = consume_apply_confirmation(raw, job_id=4, kind="SUBMIT")
    assert first is not None
    assert consume_apply_confirmation(raw, job_id=4, kind="SUBMIT") is None