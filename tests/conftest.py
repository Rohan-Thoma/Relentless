import os
import sys

import pytest

# Ensure repo root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")


@pytest.fixture(autouse=True)
def _isolate_knowledge_and_config_cache(monkeypatch, tmp_path):
    """Prevents unit tests from polluting live knowledge/ files or leaking cached configs."""
    from src.config import invalidate_config
    from src.knowledge import store as knowledge_store

    isolated_kb = tmp_path / "knowledge"
    isolated_kb.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(knowledge_store, "KNOWLEDGE_DIR", str(isolated_kb))
    # Reset global store state cleanly between tests
    s = knowledge_store.get_store()
    s.concepts.clear()
    s.dirty.clear()
    s.deleted.clear()
    s.revisions.clear()
    s._hydrated = False
    invalidate_config()
    yield
    invalidate_config()
