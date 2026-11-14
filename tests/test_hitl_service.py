"""Unit tests for Phase 1 Unified HITL Service Layer (src/hitl/service.py)."""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from src.hitl import service
from src.knowledge.store import Concept, Store
from src.pipeline import TASK_REGISTRY


def _make_fake_conn(fetchone_seq=None, fetchall_seq=None):
    fetchone_iter = iter(fetchone_seq or [])
    fetchall_iter = iter(fetchall_seq or [])
    executed = []

    class FakeCursor:
        def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))

        def fetchone(self):
            try:
                return next(fetchone_iter)
            except StopIteration:
                return None

        def fetchall(self):
            try:
                return next(fetchall_iter)
            except StopIteration:
                return []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeConn:
        def __init__(self):
            self.committed = False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            self.committed = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    conn = FakeConn()

    @contextmanager
    def _ctx():
        yield conn

    return _ctx, conn, executed


# ==============================================================================
# 1. System Status & Telemetry
# ==============================================================================
def test_get_system_status_combines_db_and_task_registry(monkeypatch):
    future_iso = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
    ctx, _conn, _executed = _make_fake_conn(
        fetchone_seq=[
            {"c": 42},
            {"c": 18},
            {"c": 9},
            {"value": future_iso},
        ],
        fetchall_seq=[
            [{"stage": "NOTIFIED", "c": 7}, {"stage": "APPLIED", "c": 5}],
            [{"human_action": "APPLIED", "c": 5}, {"human_action": "SKIP_BAD_FIT", "c": 4}],
            [{"outcome_status": "PENDING", "c": 40}, {"outcome_status": "CALLBACK", "c": 2}],
        ],
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx)

    TASK_REGISTRY.set_job_stage(77, "Tailoring ATS PDF")
    TASK_REGISTRY.set_system_task("Gmail Scanner")
    try:
        status = service.get_system_status()
    finally:
        TASK_REGISTRY.clear_job_stage(77)
        TASK_REGISTRY.clear_system_task("Gmail Scanner")

    assert status["total_jobs"] == 42
    assert status["triage_passed"] == 18
    assert status["human_labeled"] == 9
    assert status["stage_counts"] == {"NOTIFIED": 7, "APPLIED": 5}
    assert status["action_counts"] == {"APPLIED": 5, "SKIP_BAD_FIT": 4}
    assert status["outcome_counts"] == {"PENDING": 40, "CALLBACK": 2}
    assert status["active_jobs"][77] == "Tailoring ATS PDF"
    assert "Gmail Scanner" in status["system_tasks"]
    assert status["is_paused"] is True
    assert status["paused_until"] is not None


# ==============================================================================
# 2. Jobs Listing & Stage Filters
# ==============================================================================
def test_list_jobs_stage_filters_and_inflight_enrichment(monkeypatch, tmp_path):
    pdf_file = tmp_path / "resume.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 mock")

    now_dt = datetime.now(UTC)
    sample_row = {
        "id": 101,
        "dedup_hash": "abc",
        "source_ats": "greenhouse",
        "company": "Anthropic",
        "title": "ML Engineer",
        "location": "Remote",
        "apply_url": "https://boards.greenhouse.io/anthropic/jobs/101",
        "discovered_at": now_dt,
        "cosine_sim": 0.81,
        "jev_scam_prob": 0.02,
        "jev_fit_tier": "dream_role",
        "jev_fit_prob": 0.91,
        "jev_fit_prob_cal": 0.90,
        "jev_persona": "genai",
        "triage_passed": True,
        "human_action": None,
        "human_action_at": None,
        "outcome_status": "PENDING",
        "workflow_stage": "NOTIFIED",
        "apply_status": "NOT_APPLIED",
        "apply_feasible": None,
        "apply_blocker": None,
        "notified_at": now_dt,
        "tailored_pdf_path": str(pdf_file),
        "ats_scorecard": {"overall_after_pct": 88},
    }

    presets = {
        "UNREVIEWED": "triage_passed = TRUE AND human_action IS NULL",
        "IN_PROGRESS": "workflow_stage IN ('PROCESSING', 'EDITING', 'AWAITING_EDIT_NOTE', 'SUBMITTING')",
        "READY_FOR_REVIEW": "workflow_stage = 'READY_FOR_REVIEW'",
        "APPLIED": "(workflow_stage = 'APPLIED' OR human_action = 'APPLIED')",
        "CALLBACK": "(workflow_stage = 'CALLBACK' OR outcome_status IN ('CALLBACK', 'INTERVIEW', 'OFFER'))",
        "SKIPPED": "human_action IN ('SKIP_BAD_FIT', 'SKIP_SCAM')",
    }

    for stage_key, expected_sql in presets.items():
        ctx, _conn, executed = _make_fake_conn(
            fetchone_seq=[{"c": 1}],
            fetchall_seq=[[dict(sample_row)]],
        )
        monkeypatch.setattr("src.hitl.service.get_connection", ctx)

        TASK_REGISTRY.set_job_stage(101, "Inspecting application website")
        try:
            res = service.list_jobs(
                stage=stage_key,
                fit_tier="dream_role",
                source_ats="greenhouse",
                search="Anthropic",
                limit=25,
                offset=5,
            )
        finally:
            TASK_REGISTRY.clear_job_stage(101)

        assert res["total"] == 1
        assert res["limit"] == 25
        assert res["offset"] == 5
        assert len(res["items"]) == 1
        item = res["items"][0]
        assert item["in_flight_stage"] == "Inspecting application website"
        assert item["has_pdf"] is True
        assert isinstance(item["discovered_at"], str)

        count_sql, count_params = executed[0]
        assert expected_sql in count_sql
        assert "jev_fit_tier = %s" in count_sql
        assert "source_ats = %s" in count_sql
        assert "ILIKE" in count_sql
        assert count_params == ("dream_role", "greenhouse", "%Anthropic%", "%Anthropic%", "%Anthropic%")


# ==============================================================================
# 3. Job Detail Serialization
# ==============================================================================
def test_get_job_detail_serializes_cleanly(monkeypatch, tmp_path):
    shot1 = tmp_path / "step_filled.png"
    shot1.write_bytes(b"png")

    evidence_dir = tmp_path / "output" / "apply_evidence" / "55"
    evidence_dir.mkdir(parents=True)
    shot2 = evidence_dir / "step_extra.png"
    shot2.write_bytes(b"png2")

    monkeypatch.chdir(tmp_path)

    now_dt = datetime.now(UTC)
    job_row = {
        "id": 55,
        "company": "Stripe",
        "title": "AI Systems Engineer",
        "apply_url": "https://stripe.com/jobs/55",
        "discovered_at": now_dt,
        "embedding": [0.1, 0.2, 0.3],
        "tailored_resume_json": {"tailored_summary": "Summary"},
        "tailored_pdf_path": None,
        "ats_scorecard": {"overall_after_pct": 92},
        "user_edit_notes": ["Emphasize RAG"],
    }
    apply_row = {
        "id": 1,
        "job_id": 55,
        "status": "PREPARED",
        "screenshots": [str(shot1)],
        "started_at": now_dt,
    }
    outcome_row = {
        "id": 2,
        "job_id": 55,
        "kind": "CALLBACK",
        "occurred_at": now_dt,
    }

    ctx, _conn, _executed = _make_fake_conn(
        fetchone_seq=[job_row],
        fetchall_seq=[[apply_row], [outcome_row]],
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx)

    TASK_REGISTRY.set_job_stage(55, "Tailoring ATS PDF")
    try:
        detail = service.get_job_detail(55)
    finally:
        TASK_REGISTRY.clear_job_stage(55)

    assert detail is not None
    assert "embedding" not in detail
    assert isinstance(detail["discovered_at"], str)
    assert detail["has_pdf"] is True
    assert detail["in_flight_stage"] == "Tailoring ATS PDF"
    assert len(detail["apply_events"]) == 1
    assert len(detail["outcome_events"]) == 1
    assert str(shot1) in detail["screenshots"]
    assert any(p.endswith("step_extra.png") for p in detail["screenshots"])


# ==============================================================================
# 4. Record Job Actions (Skip, Manual Apply, Callback, Validation, TG Sync)
# ==============================================================================
@pytest.mark.asyncio
async def test_record_job_action_skip_manual_and_callback(monkeypatch):
    monkeypatch.setattr("src.hitl.service.run_distill", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    cleanup_calls = []
    upsert_calls = []

    async def mock_cleanup(client, chat_id, job_id, extra_mid=None):
        cleanup_calls.append((chat_id, job_id, extra_mid))

    async def mock_upsert(**kwargs):
        upsert_calls.append(kwargs)
        return 777

    monkeypatch.setattr("src.hitl.telegram_bot.cleanup_job_messages", mock_cleanup)
    monkeypatch.setattr("src.hitl.telegram_bot.upsert_job_rich_card", mock_upsert)

    # 1. SKIP_BAD_FIT -> cleans up TG card
    ctx1, conn1, exec1 = _make_fake_conn(
        fetchone_seq=[{"id": 10, "company": "Acme", "human_action": "SKIP_BAD_FIT", "active_chat_id": 12345}]
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx1)
    res_skip = await service.record_job_action(10, "skip_bad_fit", actor_source="web")
    assert res_skip["ok"] is True
    assert res_skip["action"] == "SKIP_BAD_FIT"
    assert conn1.committed is True
    assert "human_action = %s" in exec1[0][0]
    assert cleanup_calls == [(12345, 10, None)]

    # 2. MANUALLY_APPLIED -> updates TG card
    ctx2, conn2, exec2 = _make_fake_conn(
        fetchone_seq=[{"id": 11, "company": "Databricks", "workflow_stage": "APPLIED", "active_chat_id": 12345}]
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx2)
    res_manual = await service.record_job_action(11, "MANUALLY_APPLIED", actor_source="web")
    assert res_manual["ok"] is True
    assert conn2.committed is True
    assert "apply_status = 'MANUALLY_APPLIED'" in exec2[0][0]
    assert len(upsert_calls) == 1
    assert "Databricks" in upsert_calls[0]["markdown_body"]

    # 3. CALLBACK -> updates jobs & inserts outcome_events & updates TG card
    ctx3, conn3, exec3 = _make_fake_conn(
        fetchone_seq=[{"id": 12, "company": "Cohere", "outcome_status": "CALLBACK", "active_chat_id": 12345}]
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx3)
    res_cb = await service.record_job_action(12, "CALLBACK", actor_source="web")
    assert res_cb["ok"] is True
    assert conn3.committed is True
    assert len(exec3) == 2
    assert "INSERT INTO outcome_events" in exec3[1][0]
    assert len(upsert_calls) == 2

    # 4. Invalid action -> ValueError
    with pytest.raises(ValueError, match="Unsupported job action"):
        await service.record_job_action(12, "UNKNOWN_ACTION")


# ==============================================================================
# 5. Start Job Interest (Busy Guard, Initial Interest, Edit Note)
# ==============================================================================
@pytest.mark.asyncio
async def test_start_job_interest_busy_guard_and_edit_note(monkeypatch):
    monkeypatch.setattr("src.hitl.service.run_distill", lambda: None)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    # 1. Busy guard
    TASK_REGISTRY.set_job_stage(200, "Tailoring ATS PDF")
    try:
        busy_res = await service.start_job_interest(200)
    finally:
        TASK_REGISTRY.clear_job_stage(200)
    assert busy_res == {
        "ok": False,
        "error": "BUSY",
        "in_flight_stage": "Tailoring ATS PDF",
        "job_id": 200,
    }

    processed = []
    edit_notes = []

    async def mock_process(job_row, chat_id, custom_note="", is_edit=False, clicked_mid=None):
        processed.append((job_row["id"], chat_id, custom_note, is_edit, clicked_mid))

    def mock_append_note(job_id, note, chat_id=None):
        edit_notes.append((job_id, note, chat_id))

    monkeypatch.setattr("src.hitl.telegram_bot._process_job_interest", mock_process)
    monkeypatch.setattr("src.hitl.telegram_bot._append_user_edit_note", mock_append_note)

    # 2. Initial interest dispatch
    ctx1, _conn1, _exec1 = _make_fake_conn(
        fetchone_seq=[{"id": 201, "company": "OpenAI", "title": "MLE", "active_chat_id": None}]
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx1)
    res_init = await service.start_job_interest(201, actor_source="web")
    await asyncio.sleep(0.01)

    assert res_init["ok"] is True
    assert res_init["workflow_stage"] == "PROCESSING"
    assert processed[-1] == (201, 0, "", False, None)

    # 3. Edit flow with custom_note
    ctx2, _conn2, _exec2 = _make_fake_conn(
        fetchone_seq=[{"id": 201, "company": "OpenAI", "title": "MLE", "active_chat_id": 999}]
    )
    monkeypatch.setattr("src.hitl.service.get_connection", ctx2)
    res_edit = await service.start_job_interest(
        201, custom_note="Highlight LangGraph agents", is_edit=True, actor_source="web"
    )
    await asyncio.sleep(0.01)

    assert res_edit["ok"] is True
    assert res_edit["workflow_stage"] == "EDITING"
    assert edit_notes == [(201, "Highlight LangGraph agents", None)]
    assert processed[-1] == (201, 999, "Highlight LangGraph agents", True, None)


# ==============================================================================
# 6. OKF Brain & User Settings Helpers
# ==============================================================================
def test_okf_brain_and_settings_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr("src.knowledge.store.KNOWLEDGE_DIR", str(tmp_path))

    store = Store()
    store._hydrated = True
    c1 = Concept(
        path="market/hot-skills",
        concept_type="Market Insight",
        name="Hot Skills",
        tags=["genai", "python"],
        body="LangGraph and PyTorch trending",
    )
    store.upsert(c1, actor="process:test")
    store.dirty.clear()
    store.revisions.clear()

    monkeypatch.setattr("src.hitl.service.get_store", lambda: store)

    # Mock DB inside store.flush() and service settings functions
    ctx, conn, executed = _make_fake_conn()
    monkeypatch.setattr("src.knowledge.store.get_connection", ctx)
    monkeypatch.setattr("src.hitl.service.get_connection", ctx)

    brain = service.get_okf_brain_data()
    assert brain["summary"]["total_concepts"] == 1
    assert "LangGraph" in brain["summary"]["hot_skills"]
    assert brain["concepts"][0]["trust_tier"] == "unverified"

    saved = service.save_okf_concept(
        path="preferences/custom-rule",
        concept_type="Preference",
        name="Custom Rule",
        body="Prefer remote ML systems roles.",
        tags=["remote"],
        actor="human:owner",
    )
    assert saved["path"] == "preferences/custom-rule"
    assert saved["trust_tier"] == "human-reviewed"
    assert (tmp_path / "preferences" / "custom-rule.md").exists()

    # Path traversal must be rejected
    with pytest.raises(ValueError, match="Invalid OKF concept path"):
        service.save_okf_concept("../etc/passwd", "Preference", "bad")

    with pytest.raises(ValueError, match="Reserved OKF concept path"):
        service.save_okf_concept("index.md", "Preference", "bad")

    # save_master_profile
    monkeypatch.setattr(
        "src.hitl.service.LOCAL_PROFILE_PATH",
        str(tmp_path / "master_profile.json"),
    )
    prof_res = service.save_master_profile({"name": "Rohan", "email": "rohan@example.com"})
    assert prof_res["ok"] is True
    assert conn.committed is True
    assert any("INSERT INTO app_settings" in sql for sql, _ in executed)

    # set_automation_pause (pause & resume)
    pause_res = service.set_automation_pause(hours=12.0)
    assert pause_res["is_paused"] is True
    assert pause_res["paused_until"] is not None

    resume_res = service.set_automation_pause(hours=0)
    assert resume_res["is_paused"] is False
    assert resume_res["paused_until"] is None
    assert any("DELETE FROM app_settings" in sql for sql, _ in executed)
