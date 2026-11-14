"""Unit tests for Phase 2 Secure Web API and Auth (src/web/auth.py & src/web/api.py)."""

import httpx
import pytest

from src.web import auth
from src.web.api import app


@pytest.fixture(autouse=True)
def _clean_auth_state(monkeypatch):
    auth.reset_rate_limiter()
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    yield
    auth.reset_rate_limiter()


def _make_client(client_ip: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(client_ip, 50000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ==============================================================================
# 1. Public Health, Unauthenticated Rejection, and Remote vs Local Dev Guard
# ==============================================================================
@pytest.mark.asyncio
async def test_public_health_and_unauthenticated_rejection(monkeypatch):
    monkeypatch.setenv("WEB_API_KEY", "secret-key-123")

    async with _make_client() as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        auth_status = await client.get("/api/auth/status")
        assert auth_status.status_code == 200
        assert auth_status.json()["authenticated"] is False
        assert auth_status.json()["auth_required"] is True

        for ep in ("/api/status", "/api/jobs", "/api/settings", "/api/okf", "/api/eval"):
            resp = await client.get(ep)
            assert resp.status_code == 401
            assert resp.json()["error"] == "UNAUTHORIZED"

    # Without any secret configured, remote IP must still be rejected
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    async with _make_client("203.0.113.5") as remote_client:
        remote_auth = await remote_client.get("/api/auth/status")
        assert remote_auth.json()["authenticated"] is False
        assert remote_auth.json()["mode"] == "unconfigured_remote"
        remote_status = await remote_client.get("/api/status")
        assert remote_status.status_code == 401


# ==============================================================================
# 2. Netlify Edge API Key Bridge Authentication
# ==============================================================================
@pytest.mark.asyncio
async def test_netlify_edge_api_key_header_auth(monkeypatch):
    monkeypatch.setenv("WEB_API_KEY", "secret-key-123")

    async def fake_status():
        return {"total_jobs": 10, "triage_passed": 4, "active_jobs": {}, "system_tasks": []}

    monkeypatch.setattr("src.hitl.service.async_get_system_status", fake_status)

    async with _make_client("203.0.113.10") as client:
        headers = {"X-Relentless-Api-Key": "secret-key-123"}
        auth_resp = await client.get("/api/auth/status", headers=headers)
        assert auth_resp.status_code == 200
        assert auth_resp.json() == {
            "authenticated": True,
            "mode": "api_key",
            "auth_required": True,
        }

        status_resp = await client.get("/api/status", headers=headers)
        assert status_resp.status_code == 200
        assert status_resp.json()["total_jobs"] == 10


# ==============================================================================
# 3. Password Login, Session Cookie / Bearer Token, and Brute-Force Lockout
# ==============================================================================
@pytest.mark.asyncio
async def test_password_login_session_cookie_and_rate_limit_lockout(monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "super-secret-pw")

    alerts = []
    monkeypatch.setattr("src.web.auth.alert", lambda msg: alerts.append(msg))

    async def fake_status():
        return {"total_jobs": 7, "active_jobs": {}, "system_tasks": []}

    monkeypatch.setattr("src.hitl.service.async_get_system_status", fake_status)

    async with _make_client("203.0.113.99") as client:
        # Valid login -> sets cookie and returns Bearer token
        login_resp = await client.post("/api/auth/login", json={"password": "super-secret-pw"})
        assert login_resp.status_code == 200
        login_data = login_resp.json()
        assert login_data["ok"] is True
        assert login_data["mode"] == "session"
        token = login_data["token"]
        assert auth.SESSION_COOKIE_NAME in login_resp.cookies

        # Cookie auth works automatically in AsyncClient jar
        s1 = await client.get("/api/status")
        assert s1.status_code == 200
        assert s1.json()["total_jobs"] == 7

        # Logout clears cookie
        out = await client.post("/api/auth/logout")
        assert out.status_code == 200

        # Bearer token works independently
        s2 = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert s2.status_code == 200

        # 5 bad attempts trigger alert and 6th returns 429
        for i in range(5):
            bad = await client.post("/api/auth/login", json={"password": f"wrong-{i}"})
            assert bad.status_code == 401
            assert bad.json()["error"] == "INVALID_CREDENTIALS"

        assert len(alerts) == 1
        assert "5 failed auth attempts" in alerts[0]

        locked = await client.post("/api/auth/login", json={"password": "super-secret-pw"})
        assert locked.status_code == 429
        assert locked.json()["error"] == "TOO_MANY_ATTEMPTS"


# ==============================================================================
# 4. Protected Jobs, PDF/Screenshot, OKF, Eval, Settings, and Action Routes
# ==============================================================================
@pytest.mark.asyncio
async def test_jobs_okf_settings_and_actions_endpoints(monkeypatch, tmp_path):
    monkeypatch.setenv("WEB_API_KEY", "test-api-key")
    headers = {"X-Relentless-Api-Key": "test-api-key"}

    pdf_file = tmp_path / "tailored_42.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 sample")

    shot_file = tmp_path / "shot_0.png"
    shot_file.write_bytes(b"\x89PNG\r\n\x1a\nmockpng")

    async def fake_list_jobs(**kwargs):
        return {
            "items": [{"id": 42, "company": "Anthropic", "title": "AI Engineer", "stage": kwargs.get("stage")}],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    async def fake_job_detail(job_id: int):
        if job_id != 42:
            return None
        return {
            "id": 42,
            "company": "Anthropic",
            "title": "AI Engineer",
            "tailored_pdf_path": str(pdf_file),
            "screenshots": [str(shot_file)],
        }

    async def fake_ensure_pdf(job):
        return job.get("tailored_pdf_path")

    async def fake_interest(job_id, custom_note="", is_edit=False, actor_source="web", **kwargs):
        return {"ok": True, "job_id": job_id, "custom_note": custom_note, "is_edit": is_edit}

    async def fake_action(job_id, action, actor_source="web", **kwargs):
        if action not in ("SKIP_BAD_FIT", "CALLBACK", "MANUALLY_APPLIED"):
            raise ValueError(f"Unsupported job action: {action}")
        return {"ok": True, "job_id": job_id, "action": action}

    async def fake_submit(job_id, token=None, actor_source="web", **kwargs):
        return {"ok": True, "job_id": job_id, "status": "SUBMITTED"}

    async def fake_okf_get(query="", concept_type=None):
        return {"concepts": [{"path": "market/hot-skills", "body": "LangGraph"}], "summary": {"total_concepts": 1}}

    async def fake_okf_save(path, concept_type, body, name="", description="", tags=None, actor="human:owner"):
        return {"path": path, "concept_type": concept_type, "body": body}

    async def fake_okf_delete(path):
        return {"ok": True, "path": path, "deleted": True}

    async def fake_eval():
        return {"n": 12, "overall_precision": 0.75}

    async def fake_get_settings():
        return {"master_profile": {"name": "Rohan"}, "config": {}, "is_paused": False}

    async def fake_save_profile(p):
        if not isinstance(p, dict):
            raise TypeError("master_profile must be a JSON object")
        return {"ok": True, "master_profile": p}

    async def fake_save_config(c):
        return {"ok": True, "config": c}

    async def fake_pause(hours=24.0):
        return {"ok": True, "is_paused": bool(hours), "paused_until": "2026-11-15T00:00:00+00:00" if hours else None}

    async def fake_reset(mode):
        return {"ok": True, "mode": mode, "message": f"Reset {mode}"}

    monkeypatch.setattr("src.hitl.service.async_list_jobs", fake_list_jobs)
    monkeypatch.setattr("src.hitl.service.async_get_job_detail", fake_job_detail)
    monkeypatch.setattr("src.web.api.ensure_tailored_pdf", fake_ensure_pdf)
    monkeypatch.setattr("src.hitl.service.start_job_interest", fake_interest)
    monkeypatch.setattr("src.hitl.service.record_job_action", fake_action)
    monkeypatch.setattr("src.hitl.service.execute_job_submit", fake_submit)
    monkeypatch.setattr("src.hitl.service.async_get_okf_brain_data", fake_okf_get)
    monkeypatch.setattr("src.hitl.service.async_save_okf_concept", fake_okf_save)
    monkeypatch.setattr("src.hitl.service.async_delete_okf_concept", fake_okf_delete)
    monkeypatch.setattr("src.web.api.run_distill", lambda: 3)
    monkeypatch.setattr("src.hitl.service.async_get_evaluation_data", fake_eval)
    monkeypatch.setattr("src.hitl.service.async_get_user_settings", fake_get_settings)
    monkeypatch.setattr("src.hitl.service.async_save_master_profile", fake_save_profile)
    monkeypatch.setattr("src.hitl.service.async_save_config_yaml", fake_save_config)
    monkeypatch.setattr("src.hitl.service.async_set_automation_pause", fake_pause)
    monkeypatch.setattr("src.hitl.service.async_reset_database_action", fake_reset)

    async with _make_client() as client:
        # Jobs
        r_list = await client.get("/api/jobs?stage=UNREVIEWED&limit=10", headers=headers)
        assert r_list.status_code == 200
        assert r_list.json()["items"][0]["id"] == 42

        r_detail = await client.get("/api/jobs/42", headers=headers)
        assert r_detail.status_code == 200
        assert r_detail.json()["company"] == "Anthropic"

        r_missing = await client.get("/api/jobs/999", headers=headers)
        assert r_missing.status_code == 404

        r_interest = await client.post("/api/jobs/42/interest", json={}, headers=headers)
        assert r_interest.status_code == 200
        assert r_interest.json()["ok"] is True

        r_edit_bad = await client.post("/api/jobs/42/edit", json={"custom_note": ""}, headers=headers)
        assert r_edit_bad.status_code == 400

        r_edit = await client.post(
            "/api/jobs/42/edit", json={"custom_note": "Add LangGraph"}, headers=headers
        )
        assert r_edit.status_code == 200
        assert r_edit.json()["is_edit"] is True

        r_act = await client.post("/api/jobs/42/action", json={"action": "CALLBACK"}, headers=headers)
        assert r_act.status_code == 200
        assert r_act.json()["action"] == "CALLBACK"

        r_act_bad = await client.post("/api/jobs/42/action", json={"action": "BOGUS"}, headers=headers)
        assert r_act_bad.status_code == 400

        r_sub = await client.post("/api/jobs/42/submit", json={}, headers=headers)
        assert r_sub.status_code == 200
        assert r_sub.json()["status"] == "SUBMITTED"

        r_pdf = await client.get("/api/jobs/42/pdf", headers=headers)
        assert r_pdf.status_code == 200
        assert r_pdf.headers["content-type"] == "application/pdf"
        assert r_pdf.content == b"%PDF-1.4 sample"

        r_shot = await client.get("/api/jobs/42/screenshot/0", headers=headers)
        assert r_shot.status_code == 200
        assert r_shot.headers["content-type"] == "image/png"
        assert r_shot.content.startswith(b"\x89PNG")

        # OKF
        r_okf = await client.get("/api/okf", headers=headers)
        assert r_okf.status_code == 200
        assert r_okf.json()["summary"]["total_concepts"] == 1

        r_okf_put = await client.put(
            "/api/okf/preferences/test",
            json={"concept_type": "Preference", "body": "Prefer backend AI"},
            headers=headers,
        )
        assert r_okf_put.status_code == 200
        assert r_okf_put.json()["concept"]["path"] == "preferences/test"

        r_okf_del = await client.delete("/api/okf/preferences/test", headers=headers)
        assert r_okf_del.status_code == 200
        assert r_okf_del.json()["deleted"] is True

        r_distill = await client.post("/api/okf/distill", json={}, headers=headers)
        assert r_distill.status_code == 200
        assert r_distill.json()["distilled"] == 3

        # Eval & Settings
        r_eval = await client.get("/api/eval", headers=headers)
        assert r_eval.status_code == 200
        assert r_eval.json()["n"] == 12

        r_set = await client.get("/api/settings", headers=headers)
        assert r_set.status_code == 200
        assert r_set.json()["master_profile"]["name"] == "Rohan"

        r_prof = await client.put(
            "/api/settings/profile", json={"name": "Rohan Thoma"}, headers=headers
        )
        assert r_prof.status_code == 200
        assert r_prof.json()["master_profile"]["name"] == "Rohan Thoma"

        r_cfg = await client.put(
            "/api/settings/config", json={"automation": {"apply_mode": "DRY_RUN"}}, headers=headers
        )
        assert r_cfg.status_code == 200

        # Actions
        r_pause = await client.post("/api/actions/pause", json={"hours": 12}, headers=headers)
        assert r_pause.status_code == 200
        assert r_pause.json()["is_paused"] is True

        r_reset_unconfirmed = await client.post(
            "/api/actions/reset", json={"mode": "labels"}, headers=headers
        )
        assert r_reset_unconfirmed.status_code == 400

        r_reset = await client.post(
            "/api/actions/reset", json={"mode": "labels", "confirm": True}, headers=headers
        )
        assert r_reset.status_code == 200
        assert r_reset.json()["mode"] == "labels"
