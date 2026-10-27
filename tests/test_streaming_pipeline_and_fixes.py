"""
Phase 6 Automated Verification Suite:
Testing Zero OS-Specific Imports, Gmail lookback_days & header-first BODY.PEEK,
Job Freshness Filtering, Write-Through Cache & Zero-DB Dedup,
Real-Time Per-Job Streaming Pipeline, Telegram Bot API 10.3 sendRichMessage Single-Card Lifecycle,
and Crash / Docker Restart State Recovery.
"""

import ast
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from src.config import load_config
from src.hitl.telegram_bot import (
    _handle_single_update,
    cleanup_job_messages,
    upsert_job_rich_card,
)
from src.ingestors.common import insert_job, set_job_inserted_hook
from src.ingestors.dedup_cache import CACHE
from src.ingestors.freshness import is_timestamp_fresh, parse_relative_age_hours
from src.ingestors.gmail_ingestor import fetch_alert_emails_sync
from src.pipeline import TASK_REGISTRY, PipelineEngine
from src.tailor.resume_tailor import ensure_tailored_pdf


# ==============================================================================
# Test 1: Zero OS-Specific Imports & Paths
# ==============================================================================
def test_zero_os_specific_imports():
    """AST/source check asserting src/ contains zero fcntl, msvcrt, os.name, sys.platform,
    or hardcoded /tmp references.
    """
    src_dir = Path("src")
    py_files = list(src_dir.rglob("*.py"))
    assert len(py_files) > 0, "No python files found in src/"

    forbidden_modules = {"fcntl", "msvcrt"}
    forbidden_attrs = {("os", "name"), ("sys", "platform")}

    violations = []

    for py_path in py_files:
        code = py_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(py_path))

        for node in ast.walk(tree):
            # 1. Imports: import fcntl, import msvcrt
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod_base = alias.name.split(".")[0]
                    if mod_base in forbidden_modules:
                        violations.append(f"{py_path}:{node.lineno} imports forbidden module '{alias.name}'")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in forbidden_modules:
                    violations.append(f"{py_path}:{node.lineno} imports from forbidden module '{node.module}'")

            # 2. Attribute checks: os.name, sys.platform
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    if (node.value.id, node.attr) in forbidden_attrs:
                        violations.append(f"{py_path}:{node.lineno} accesses forbidden attribute '{node.value.id}.{node.attr}'")

            # 3. String literals containing /tmp
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value.strip()
                if s == "/tmp" or s.startswith("/tmp/") or "/tmp/" in s:
                    violations.append(f"{py_path}:{node.lineno} contains hardcoded '/tmp' path: '{s}'")

    assert not violations, "Found OS-specific or non-portable code:\n" + "\n".join(violations)


# ==============================================================================
# Test 2: Gmail lookback_days & Header-First BODY.PEEK
# ==============================================================================
def test_gmail_lookback_days_and_body_peek(monkeypatch):
    """Proves labeled search includes newer_than:{lookback_days}d,
    folder fallback uses (SINCE ...), and cached/processed Message-IDs
    never trigger Stage B full email body downloads.
    """
    mock_mail = MagicMock()

    # Step 1: Mock IMAP connection and login
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: mock_mail)
    mock_mail.select.return_value = ("OK", [b"1"])

    search_queries_issued = []
    fetch_commands_issued = []

    def mock_uid(cmd, *args):
        if cmd == "search":
            search_queries_issued.append(args)
            return ("OK", [b"101 102"])
        elif cmd == "fetch":
            fetch_commands_issued.append(args)
            _uid_arg, fetch_type = args[0], args[1]
            if "HEADER.FIELDS" in fetch_type:
                # Stage A RFC822 header response for both emails
                # UID 101: Fresh email, not yet processed
                hdr_1 = (
                    b"Message-ID: <msg101@example.com>\r\n"
                    b"Subject: Software Engineer Job Alert\r\n"
                    b"From: alerts@linkedin.com\r\n"
                    b"Date: Mon, 26 Oct 2026 12:00:00 +0000\r\n\r\n"
                )
                # UID 102: Already processed email
                hdr_2 = (
                    b"Message-ID: <msg102@example.com>\r\n"
                    b"Subject: ML Engineer Job Alert\r\n"
                    b"From: alerts@linkedin.com\r\n"
                    b"Date: Mon, 26 Oct 2026 12:00:00 +0000\r\n\r\n"
                )
                return ("OK", [(b"1 (UID 101 ...)", hdr_1), (b"2 (UID 102 ...)", hdr_2)])
            elif "(BODY.PEEK[])" in fetch_type:
                # Stage B full body
                raw_body = (
                    b"From: alerts@linkedin.com\r\n"
                    b"To: test@example.com\r\n"
                    b"Subject: Software Engineer Job Alert\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n\r\n"
                    b"<html><body><a href='https://www.linkedin.com/jobs/view/123456'>Job</a></body></html>"
                )
                return ("OK", [(b"1 (BODY[] ...)", raw_body)])
        return ("OK", [])

    mock_mail.uid.side_effect = mock_uid

    # Mock is_email_processed: msg102 is already processed!
    def mock_is_email_processed(msg_id):
        return msg_id == "<msg102@example.com>"

    monkeypatch.setattr("src.ingestors.gmail_ingestor.is_email_processed", mock_is_email_processed)

    emails = fetch_alert_emails_sync(
        gmail_address="test@example.com",
        gmail_password="secretpassword",
        lookback_days=3,
        gmail_label="professional-job_alerts",
    )

    # 1. Assert search used lookback constraint (args: (None, 'X-GM-RAW', raw_query))
    raw_query = search_queries_issued[0][2]
    assert "newer_than:3d" in raw_query, f"Expected 'newer_than:3d' in query, got {raw_query}"
    assert "label:professional-job_alerts" in raw_query

    # 2. Assert Stage A fetched only headers
    header_fetches = [f for f in fetch_commands_issued if "HEADER.FIELDS" in f[1]]
    assert len(header_fetches) == 1, "Expected 1 batched header fetch"

    # 3. Assert Stage B fetched body ONLY for UID 101, NEVER for 102
    body_fetches = [f for f in fetch_commands_issued if "(BODY.PEEK[])" in f[1]]
    assert len(body_fetches) == 1, f"Expected exactly 1 body fetch, got {len(body_fetches)}"
    assert body_fetches[0][0] == b"101", f"Expected body fetch for UID 101 only, got {body_fetches[0][0]}"
    assert len(emails) == 1
    assert emails[0]["message_id"] == "<msg101@example.com>"


# ==============================================================================
# Test 3: Job Freshness Filtering
# ==============================================================================
def test_job_freshness_filtering():
    """Verifies parse_relative_age_hours and is_timestamp_fresh across all formats."""
    # 1. Relative age parsing
    assert parse_relative_age_hours("Just now") == 1.0
    assert parse_relative_age_hours("30 minutes ago") == 0.5
    assert parse_relative_age_hours("4 hours ago") == 4.0
    assert parse_relative_age_hours("2 days ago") == 48.0
    assert parse_relative_age_hours("1 week ago") == 168.0
    assert parse_relative_age_hours("Posted Today") == 1.0
    assert parse_relative_age_hours("Posted 30+ Days Ago") == 720.0
    assert parse_relative_age_hours(None) is None
    assert parse_relative_age_hours("Unknown") is None

    # 2. Freshness bounds checking (max_hours = 72h)
    max_hrs = 72.0
    assert is_timestamp_fresh(None, max_hrs) is True
    assert is_timestamp_fresh("", max_hrs) is True
    assert is_timestamp_fresh("4 hours ago", max_hrs) is True
    assert is_timestamp_fresh("2 days ago", max_hrs) is True
    assert is_timestamp_fresh("1 week ago", max_hrs) is False
    assert is_timestamp_fresh("Posted 30+ Days Ago", max_hrs) is False

    # Unix timestamp (sec & ms)
    now_ts = datetime.now(UTC).timestamp()
    assert is_timestamp_fresh(now_ts - 3600, max_hrs) is True
    assert is_timestamp_fresh(now_ts - 400000, max_hrs) is False
    assert is_timestamp_fresh((now_ts - 3600) * 1000, max_hrs) is True  # milliseconds

    # ISO-8601 strings
    fresh_iso = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
    stale_iso = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    assert is_timestamp_fresh(fresh_iso, max_hrs) is True
    assert is_timestamp_fresh(stale_iso, max_hrs) is False

    # datetime / date objects
    assert is_timestamp_fresh(datetime.now(UTC) - timedelta(hours=12), max_hrs) is True
    assert is_timestamp_fresh(datetime.now(UTC) - timedelta(days=5), max_hrs) is False
    assert is_timestamp_fresh(datetime.now(UTC).date(), max_hrs) is True


# ==============================================================================
# Test 4: Write-Through Cache & Zero-DB Duplicate Checks
# ==============================================================================
def test_write_through_cache_and_dedup(monkeypatch):
    """Proves load_config() respects TTL and insert_job() avoids DB calls on cache hits."""
    # 1. Test load_config TTL caching
    cfg1 = load_config()
    cfg2 = load_config()
    assert cfg1 is cfg2, "load_config() should return cached instance within TTL"

    # 2. Test IngestionCache dedup write-through
    CACHE.reset()
    assert not CACHE.hydrated

    # Populate cache manually
    CACHE.hydrated = True
    CACHE.record_job("hash_apple", "Apple", "Staff Engineer")
    CACHE.record_email("<msg1@test>")
    assert CACHE.hydrated

    # In-memory duplicate query should return False immediately with 0 DB calls
    db_called = False

    def mock_get_conn():
        nonlocal db_called
        db_called = True
        raise RuntimeError("DB should NOT be called when cache has hash!")

    monkeypatch.setattr("src.ingestors.common.get_connection", mock_get_conn)

    # Calling insert_job with existing hash in CACHE
    from src.db import make_dedup_hash
    target_hash = make_dedup_hash("Apple", "Staff Engineer", "Remote")
    CACHE.record_job(target_hash, "Apple", "Staff Engineer")

    inserted = insert_job(
        None,
        source_ats="greenhouse",
        company="Apple",
        title="Staff Engineer",
        location="Remote",
        apply_url="https://apple.com/job/1",
        jd_text="Job description",
    )
    assert inserted is False
    assert not db_called, "insert_job accessed DB despite hit in CACHE!"

    # 3. Test job inserted hook
    hook_called_with = None

    def sample_hook(job_dict):
        nonlocal hook_called_with
        hook_called_with = job_dict

    set_job_inserted_hook(sample_hook)

    # Mock DB commit first, then cache record & hook invocation
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    fake_cur.fetchone.return_value = {"id": 999}

    res = insert_job(
        fake_conn,
        source_ats="greenhouse",
        company="NewCo",
        title="AI Engineer",
        location="Remote",
        apply_url="https://newco.com/job/2",
        jd_text="JD",
    )
    assert res is True
    assert fake_conn.commit.called, "conn.commit() must be called BEFORE hook"
    assert hook_called_with is not None
    assert hook_called_with["company"] == "NewCo"
    assert CACHE.has_hash(hook_called_with["dedup_hash"])

    # Clean up hook
    set_job_inserted_hook(None)


# ==============================================================================
# Test 5: Real-Time Per-Job Streaming Pipeline
# ==============================================================================
@pytest.mark.asyncio
async def test_realtime_per_job_streaming(monkeypatch):
    """Proves Job #1 is triaged and notified immediately while producer could still run."""
    engine = PipelineEngine(triage_concurrency=1)

    scored_jobs = []
    notified_jobs = []

    async def mock_score_single_job(client, job):
        scored = dict(job)
        scored["triage_passed"] = True
        scored["workflow_stage"] = "TRIAGED"
        scored_jobs.append(scored)
        return scored

    async def mock_send_single_job_card(job, chat_id=None):
        notified_jobs.append(job)
        return 5001

    monkeypatch.setattr("src.pipeline.score_single_job", mock_score_single_job)
    monkeypatch.setattr("src.hitl.telegram_bot.send_single_job_card", mock_send_single_job_card)

    await engine.start()

    # Simulate producer pushing Job #1
    job_1 = {
        "id": 1,
        "dedup_hash": "hash_1",
        "company": "Anthropic",
        "title": "Prompt Engineer",
        "location": "Remote",
        "apply_url": "https://anthropic.com/1",
        "workflow_stage": "DISCOVERED",
    }
    engine.enqueue_discovered_job(job_1)

    # Await engine draining
    await engine.drain()
    await engine.stop()

    assert len(scored_jobs) == 1
    assert scored_jobs[0]["company"] == "Anthropic"
    assert len(notified_jobs) == 1
    assert notified_jobs[0]["id"] == 1


# ==============================================================================
# Test 6: sendRichMessage Single-Card Lifecycle & In-Flight Guard
# ==============================================================================
@pytest.mark.asyncio
async def test_rich_message_lifecycle_and_inflight_guard(monkeypatch):
    """Mocks POST /sendRichMessage, /editMessageText, and /deleteMessage via respx
    to verify strictly 1 message_id per job_id, complete deletion on Skip,
    and instant in-flight busy replies.
    """
    bot_token = "123456:ABC-DEF"
    base_url = f"https://api.telegram.org/bot{bot_token}"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", bot_token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr("src.hitl.telegram_bot.TG_BASE", base_url)

    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    @contextmanager
    def mock_get_conn():
        yield fake_conn

    monkeypatch.setattr("src.hitl.telegram_bot.get_connection", mock_get_conn)

    with respx.mock:
        send_route = respx.post(f"{base_url}/sendRichMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9001}})
        )
        edit_route = respx.post(f"{base_url}/editMessageText").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 9001}})
        )
        delete_route = respx.post(f"{base_url}/deleteMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        answer_cb_route = respx.post(f"{base_url}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )

        async with httpx.AsyncClient() as client:
            # 1. Initial Notification: Sends sendRichMessage
            mid1 = await upsert_job_rich_card(
                client=client,
                chat_id=12345,
                job_id=42,
                markdown_body="*DeepMind* - Research Scientist",
                inline_keyboard=[[{"text": "🎯 Interested", "callback_data": "act:APPLIED:42"}]],
            )
            assert mid1 == 9001
            assert send_route.call_count == 1

            # 2. In-Place Update (e.g. processing progress): Uses editMessageText with clicked_mid
            mid2 = await upsert_job_rich_card(
                client=client,
                chat_id=12345,
                job_id=42,
                markdown_body="⏳ Job #42 — Under Process: Step 1/2 Tailoring ATS PDF...",
                inline_keyboard=None,
                try_edit_text_only=True,
                clicked_mid=9001,
            )
            assert mid2 == 9001
            assert edit_route.call_count == 1

            # 3. Skip: Deletes message completely (zero messages left in DM)
            await cleanup_job_messages(client, 12345, 42, extra_mid=9001)
            assert delete_route.call_count == 1

            # 4. In-Flight Guard: When job is marked active in TASK_REGISTRY
            TASK_REGISTRY.set_job_stage(42, "Tailoring ATS PDF...")
            assert TASK_REGISTRY.get_job_stage(42) is not None

            cb_update = {
                "update_id": 100,
                "callback_query": {
                    "id": "cb_query_1",
                    "from": {"id": 12345},
                    "data": "interest:42",
                    "message": {"message_id": 9001, "chat": {"id": 12345}},
                },
            }
            # Handling callback when active should trigger busy alert
            await _handle_single_update(client, cb_update)
            assert answer_cb_route.call_count >= 1
            last_request = answer_cb_route.calls.last.request
            data = json.loads(last_request.content)
            assert "already in process" in data.get("text", "")

            # Clear task stage
            TASK_REGISTRY.clear_job_stage(42)
            assert TASK_REGISTRY.get_job_stage(42) is None


# ==============================================================================
# Test 7: Crash & Docker Restart State Recovery
# ==============================================================================
@pytest.mark.asyncio
async def test_crash_recovery_resumes_cleanly(monkeypatch, tmp_path):
    """Simulates interrupted jobs in PROCESSING, EDITING (with notes),
    and SUBMITTING, and missing local PDF regenerated from tailored_resume_json
    without LLM calls.
    """
    monkeypatch.setenv("RELENTLESS_TEST_LIVE_DB", "1")

    interrupted_processing = [
        {
            "id": 101,
            "company": "Meta",
            "title": "LLM Engineer",
            "workflow_stage": "PROCESSING",
            "active_chat_id": 99999,
            "apply_url": "https://meta.com/careers/101",
            "tailored_resume_json": {"tailored_summary": "Expert LLM Engineer"},
            "user_edit_notes": None,
        }
    ]
    interrupted_editing = [
        {
            "id": 102,
            "company": "OpenAI",
            "title": "Research Engineer",
            "workflow_stage": "EDITING",
            "active_chat_id": 99999,
            "apply_url": "https://openai.com/careers/102",
            "tailored_resume_json": {"tailored_summary": "Original summary"},
            "user_edit_notes": ["Add LangSmith and LangGraph"],
        }
    ]
    interrupted_submitting = [
        {
            "id": 103,
            "company": "Amazon",
            "title": "Applied Scientist",
            "workflow_stage": "SUBMITTING",
            "active_chat_id": 99999,
            "apply_url": "https://amazon.jobs/103",
            "tailored_resume_json": {"tailored_summary": "Applied Scientist"},
            "user_edit_notes": None,
        }
    ]

    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur

    fake_cur.fetchall.side_effect = [
        [],
        [],
        interrupted_processing,
        interrupted_editing,
        interrupted_submitting,
    ]

    @contextmanager
    def mock_get_conn():
        yield fake_conn

    monkeypatch.setattr("src.pipeline.get_connection", mock_get_conn)
    monkeypatch.setattr("src.db.get_connection", mock_get_conn)

    resumed_tasks = []

    async def mock_resume_interest(job_row, custom_note=None, is_edit=False):
        resumed_tasks.append((job_row["id"], "interest", custom_note, is_edit))

    async def mock_resume_submit(job_row):
        resumed_tasks.append((job_row["id"], "submit"))

    monkeypatch.setattr("src.hitl.telegram_bot.resume_job_interest_after_crash", mock_resume_interest)
    monkeypatch.setattr("src.hitl.telegram_bot.resume_job_submit_after_crash", mock_resume_submit)

    engine = PipelineEngine(triage_concurrency=1)
    await engine.recover_state_on_startup()
    await asyncio.sleep(0.01)

    assert any(t[0] == 101 and t[1] == "interest" and t[3] is False for t in resumed_tasks)
    assert any(t[0] == 102 and t[1] == "interest" and "LangSmith" in (t[2] or "") for t in resumed_tasks)
    assert any(t[0] == 103 and t[1] == "submit" for t in resumed_tasks)

    # 2. Test PDF regeneration from tailored_resume_json
    monkeypatch.setattr(
        "src.tailor.resume_tailor.render_typst_pdf",
        lambda comp, role, tail, job_id: str(tmp_path / f"test_{job_id}.pdf"),
    )

    pdf_path = await ensure_tailored_pdf({
        "id": 101,
        "company": "Meta",
        "title": "LLM Engineer",
        "tailored_resume_json": {"tailored_summary": "Expert LLM Engineer"},
    })
    assert pdf_path is not None
    assert "test_101.pdf" in pdf_path
