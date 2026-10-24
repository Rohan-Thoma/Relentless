
import pytest

from dotenv import load_dotenv
from src.db import get_connection

load_dotenv(override=True)

from src.db import (
    consume_pending_action,
    create_pending_action,
    get_pending_action,
)


def _db_available():
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="database unavailable")
def test_pending_action_consume_once():
    raw, _ = create_pending_action("DESTRUCTIVE", {"mode": "all", "chat_id": 1}, ttl_seconds=60)
    assert get_pending_action(raw) is not None
    row = consume_pending_action(raw, kind="DESTRUCTIVE")
    assert row is not None
    # reused token fails
    assert consume_pending_action(raw, kind="DESTRUCTIVE") is None


@pytest.mark.skipif(not _db_available(), reason="database unavailable")
def test_pending_action_expired():
    raw, _ = create_pending_action("DESTRUCTIVE", {"mode": "all"}, ttl_seconds=-1)
    assert consume_pending_action(raw, kind="DESTRUCTIVE") is None


@pytest.mark.skipif(not _db_available(), reason="database unavailable")
def test_pending_action_wrong_kind():
    raw, _ = create_pending_action("DESTRUCTIVE", {"mode": "all"}, ttl_seconds=60)
    assert consume_pending_action(raw, kind="OTHER") is None
    # token should still be there (not consumed)
    assert get_pending_action(raw) is not None
    consume_pending_action(raw)  # cleanup