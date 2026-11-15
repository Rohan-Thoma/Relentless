"""Tests for open-source readiness, secret-redacting logging, Telegram /logs, and dynamic resume PDF."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.config import AppConfig
from src.hitl import telegram_bot
from src.ingestors.jobboards_poller import poll_jobboards
from src.logging_config import (
    clear_log_buffer,
    configure_logging,
    format_logs_text,
    get_recent_logs,
    redact_secrets,
)
from src.readiness import collect_readiness
from src.tailor import resume_tailor
from src.web import auth
from src.web.api import app


@pytest.fixture(autouse=True)
def _reset_logs_and_auth(monkeypatch):
    clear_log_buffer()
    auth.reset_rate_limiter()
    monkeypatch.delenv("WEB_API_KEY", raising=False)
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    yield
    clear_log_buffer()
    auth.reset_rate_limiter()


def test_secret_redaction_and_ring_buffer_filters():
    configure_logging(capture_stdio=False)
    # Verify idempotent configure_logging attaches at most 1 ring handler
    import logging

    configure_logging(capture_stdio=False)
    ring_handlers = [
        h for h in logging.getLogger().handlers if type(h).__name__ == "_RingBufferHandler"
    ]
    assert len(ring_handlers) == 1

    raw = (
        "postgres://alice:super_secret_pw@ep-foo.neon.tech/db "
        "sk-or-v1-1234567890abcdef sk-proj-abcdef1234567890abcd "
        "Authorization: Bearer mytopsecretbearertoken123 "
        "https://api.telegram.org/bot99887766:AAFakeSecretTokenValue1234567890/sendMessage"
    )
    redacted = redact_secrets(raw)
    assert "super_secret_pw" not in redacted
    assert "sk-or-v1-1234567890abcdef" not in redacted
    assert "sk-proj-abcdef1234567890abcd" not in redacted
    assert "mytopsecretbearertoken123" not in redacted
    assert "AAFakeSecretTokenValue1234567890" not in redacted
    assert "postgres://alice:***@ep-foo.neon.tech/db" in redacted

    test_logger = logging.getLogger("relentless.test_logs")
    test_logger.setLevel(logging.DEBUG)
    test_logger.info("info message sk-or-v1-abcdef1234567890")
    test_logger.warning("warning message for company Acme")
    test_logger.error("error connecting with postgres://u:pw123@host/db")

    all_logs = get_recent_logs(limit=50)
    assert all_logs["counts_by_level"]["ERROR"] >= 1
    assert all_logs["counts_by_level"]["WARNING"] >= 1
    assert any("sk-or-v1-***" in e["message"] for e in all_logs["entries"])
    assert all("pw123" not in e["message"] for e in all_logs["entries"])

    err_only = get_recent_logs(limit=50, level="ERROR")
    assert all(e["level"] == "ERROR" for e in err_only["entries"])
    assert len(err_only["entries"]) >= 1

    q_only = get_recent_logs(limit=50, query="Acme")
    assert len(q_only["entries"]) == 1
    assert "Acme" in q_only["entries"][0]["message"]

    newer = get_recent_logs(limit=50, since_id=q_only["entries"][0]["id"])
    assert all(e["id"] > q_only["entries"][0]["id"] for e in newer["entries"])

    formatted = format_logs_text(limit=10, level="ERROR")
    assert "ERROR" in formatted


@pytest.mark.asyncio
async def test_web_logs_and_profile_template_endpoints(monkeypatch):
    monkeypatch.setenv("WEB_API_KEY", "unit-test-secret")
    configure_logging(capture_stdio=False)
    import logging

    logging.getLogger("relentless.webtest").error("simulated backend failure")

    monkeypatch.setattr(
        "src.web.api.collect_readiness",
        lambda check_db=True: {
            "ready": False,
            "db_ok": True,
            "profile_configured": False,
            "missing_required": ["MASTER_PROFILE"],
            "warnings": [],
            "checks": [
                {
                    "id": "master_profile",
                    "label": "Candidate Master Profile",
                    "status": "error",
                    "detail": "Not configured",
                    "fix": "Set profile in 06 // CONFIG & VAULT",
                }
            ],
        },
    )

    transport = httpx.ASGITransport(app=app, client=("203.0.113.5", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauth = await client.get("/api/logs")
        assert unauth.status_code == 401

        headers = {"X-Relentless-Api-Key": "unit-test-secret"}
        resp = await client.get("/api/logs?level=ERROR&limit=20", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["readiness"]["profile_configured"] is False
        assert any("simulated backend failure" in e["message"] for e in body["entries"])

        tmpl_resp = await client.get("/api/settings/profile-template", headers=headers)
        assert tmpl_resp.status_code == 200
        assert tmpl_resp.json()["template"]["name"] == "Alex Developer"

        clr = await client.post("/api/logs/clear", json={}, headers=headers)
        assert clr.status_code == 200
        assert clr.json()["ok"] is True


@pytest.mark.asyncio
async def test_telegram_logs_command_plain_text_and_chunked(monkeypatch):
    configure_logging(capture_stdio=False)
    import logging

    long_msg = "failure_with_underscores_" + ("x" * 4500)
    logging.getLogger("relentless.tgtest").error(long_msg)

    sent_payloads = []

    class FakeClient:
        async def post(self, url, json=None, **kwargs):
            sent_payloads.append(json)
            return SimpleNamespace(status_code=200)

    await telegram_bot.handle_slash_command(FakeClient(), "/logs error 10", chat_id=12345)
    assert len(sent_payloads) >= 2
    # Must omit parse_mode so underscores inside tracebacks never fail Telegram API
    assert all("parse_mode" not in p for p in sent_payloads)
    combined = "\n".join(p["text"] for p in sent_payloads)
    assert "failure_with_underscores_" in combined


@pytest.mark.asyncio
async def test_telegram_listener_requires_both_token_and_chat_id(monkeypatch):
    monkeypatch.setattr(telegram_bot, "BOT_TOKEN", "")
    monkeypatch.setattr(telegram_bot, "CHAT_ID", "")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        await telegram_bot.listen_for_button_clicks()


def test_dynamic_render_typst_pdf_handles_one_role_zero_edu_and_candidate_filename(
    monkeypatch, tmp_path
):
    captured = {}

    def fake_compile(typ_path, output):
        captured["typ_path"] = typ_path
        captured["pdf_path"] = output
        captured["typ_source"] = Path(typ_path).read_text(encoding="utf-8")
        Path(output).write_bytes(b"%PDF-1.4\n%test")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(resume_tailor.typst, "compile", fake_compile)
    monkeypatch.setattr(
        resume_tailor,
        "get_master_profile",
        lambda: {
            "name": "Ada Lovelace",
            "email": "ada@example.org",
            "location": "London, UK",
            "core_skills": ["Python", "Mathematics"],
            "experience": [
                {
                    "company": "Analytical Engines Ltd",
                    "role": "Lead Algorithm Architect",
                    "dates": "1842 - 1843",
                    "bullets": ["Designed the first Bernoulli sequence algorithm."],
                }
            ],
            "education": [],
        },
    )

    out_pdf = resume_tailor.render_typst_pdf(
        company="Babbage Labs",
        role_title="Research Engineer",
        tailored={
            "tailored_summary": "Pioneer in symbolic computation.",
            "matched_keywords": ["Python"],
            "tailored_bullets_by_role": [["Authored verified Bernoulli computation algorithm."]],
        },
        job_id=77,
    )

    assert os.path.basename(out_pdf) == "Ada_Lovelace_Babbage_Labs_77.pdf"
    assert "Rohan" not in out_pdf
    assert "Ada Lovelace" in captured["typ_source"]
    assert "Analytical Engines Ltd" in captured["typ_source"]
    assert "EDUCATION" not in captured["typ_source"]


def test_render_typst_pdf_fails_loudly_without_experience(monkeypatch):
    monkeypatch.setattr(
        resume_tailor,
        "get_master_profile",
        lambda: {"name": "New User", "email": "u@example.com", "experience": []},
    )
    with pytest.raises(ValueError, match="at least 1 experience entry"):
        resume_tailor.render_typst_pdf("Acme", "Engineer", {"tailored_summary": "Hi"})


def test_readiness_diagnostics_never_leaks_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:super_secret_db_pw@host/db")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-verysecret123456")

    rep = collect_readiness(check_db=False)
    serialized = str(rep)
    assert "super_secret_db_pw" not in serialized
    assert "sk-proj-verysecret123456" not in serialized
    assert "OPENROUTER_API_KEY" in rep["missing_required"]


@pytest.mark.asyncio
async def test_jobboards_fails_loudly_when_empty(monkeypatch):
    monkeypatch.setattr(
        "src.ingestors.jobboards_poller.get_jobboards_parameters",
        lambda **kwargs: ([], [], ["linkedin"], 10, 24),
    )
    with pytest.raises(ValueError, match="cannot run without search_terms and locations"):
        await poll_jobboards()


def test_jobboards_config_supports_country_indeed():
    cfg = AppConfig()
    assert cfg.jobboards.country_indeed == "india"
    cfg_us = AppConfig.model_validate({"jobboards": {"country_indeed": "usa"}})
    assert cfg_us.jobboards.country_indeed == "usa"


def test_no_personal_hardcodes_or_disallowed_css():
    root = Path(__file__).resolve().parents[1]
    assert "DEFAULT_BACKEND_URL" not in (root / "netlify/edge-functions/api_proxy.ts").read_text()
    tailor_src = (root / "src/tailor/resume_tailor.py").read_text()
    for personal in ("Rohan_Vailala_Thoma", "Kynea Solutions", "Starinco"):
        assert personal not in tailor_src

    styles = (root / "web/styles.css").read_text()
    assert "box-shadow" not in styles
    assert "backdrop-filter" not in styles

    index_html = (root / "web/index.html").read_text()
    for elem_id in ("view-logs", "onboarding-banner", "btn-pf-load-template", "ec-jb-country-indeed"):
        assert elem_id in index_html

    app_js = (root / "web/app.js").read_text()
    assert '"logs"' in app_js
