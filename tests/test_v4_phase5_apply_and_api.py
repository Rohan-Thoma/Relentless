"""Phase 5 Verification Tests: Apply Modes, Honest DOM Detector, LangGraph ApplyAgentGraph,
Structured Skip Reasons + Learning Gate, Model Catalog, Location Policy Tester & Experiments API.
"""

from __future__ import annotations

import httpx
from playwright.async_api import async_playwright
import pytest

from src.apply.detector import classify, detect_aggregator_shell
from src.apply.llm_form_agent import extract_form_schema
from src.apply.runner import submit_application
from src.config import AppConfig
from src.hitl.telegram_bot import build_inline_keyboard, build_skip_reason_keyboard
from src.orchestrator.graph import run_apply_agent_graph
from src.web import auth
from src.web.api import app


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch):
    auth.reset_rate_limiter()
    monkeypatch.setenv("WEB_API_KEY", "phase5-test-key")
    yield
    auth.reset_rate_limiter()


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Relentless-Api-Key": "phase5-test-key"},
    )


# ==============================================================================
# 1. Apply Mode Enforcement (PREPARE_ONLY & MANUAL_ONLY Hard-Block Submissions)
# ==============================================================================
@pytest.mark.asyncio
async def test_prepare_only_and_manual_only_hard_block_submit(monkeypatch):
    consumed_called = False

    def _boom(*args, **kwargs):
        nonlocal consumed_called
        consumed_called = True
        raise AssertionError("Should never consume confirmation token in PREPARE_ONLY/MANUAL_ONLY mode!")

    monkeypatch.setattr("src.apply.runner.consume_apply_confirmation", _boom)

    job = {
        "id": 401,
        "company": "Stripe",
        "title": "AI Engineer",
        "apply_url": "https://boards.greenhouse.io/stripe/jobs/401",
    }

    for mode in ("PREPARE_ONLY", "DRY_RUN", "MANUAL_ONLY", "MANUAL"):
        cfg = AppConfig.model_validate({"automation": {"apply_mode": mode}})
        monkeypatch.setattr("src.apply.runner.load_config", lambda cfg=cfg: cfg)
        res = await submit_application(job, "fake-token")
        assert res["status"] == "DRY_RUN_BLOCKED"
        assert consumed_called is False

        # Also verify via LangGraph APPLY_AGENT_GRAPH
        gres = await run_apply_agent_graph(job, action="SUBMIT", confirmation_token="fake-token")
        assert gres["status"] == "DRY_RUN_BLOCKED"
        assert consumed_called is False


# ==============================================================================
# 2. Fast Aggregator Shell Detection (LinkedIn, Naukri, Indeed, Workday)
# ==============================================================================
def test_aggregator_shell_detection_skips_headless_playwright():
    for url in (
        "https://www.linkedin.com/jobs/view/4123456789/",
        "https://www.naukri.com/job-listings-ai-engineer-12345",
        "https://in.indeed.com/viewjob?jk=abc123",
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/AI-Engineer",
    ):
        res = detect_aggregator_shell(url)
        assert res is not None, f"Expected shell detection for {url}"
        assert res[0] == "MANUAL_APPLY"

    assert detect_aggregator_shell("https://boards.greenhouse.io/stripe/jobs/123") is None
    assert detect_aggregator_shell("https://jobs.lever.co/mistral/abc") is None


# ==============================================================================
# 3. Real DOM Detector: Search Bars Ignored, Essays Enforced, CAPTCHA Detected
# ==============================================================================
@pytest.mark.asyncio
async def test_detector_never_misclassifies_nav_search_bar_as_application_form(tmp_path):
    html = tmp_path / "search_only_landing.html"
    html.write_text(
        """
        <html><body>
          <header>
            <nav>
              <form role="search">
                <input type="search" name="q" placeholder="Search jobs" />
                <button type="submit">Search</button>
              </form>
              <a href="/login">Sign in</a>
            </nav>
          </header>
          <main>
            <h1>AI Engineer — Wipro</h1>
            <p>Great opportunity to work on ML systems.</p>
          </main>
        </body></html>
        """,
        encoding="utf-8",
    )
    url = f"file://{html.resolve()}"
    status, _reason = await classify(url)
    assert status == "NEEDS_REVIEW"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)
        schema = await extract_form_schema(page)
        await browser.close()
    assert schema == []


@pytest.mark.asyncio
async def test_detector_enforces_max_free_text_questions(monkeypatch, tmp_path):
    html = tmp_path / "essay_heavy_form.html"
    html.write_text(
        """
        <html><body>
          <main>
            <form>
              <input type="text" name="first_name" placeholder="First Name" />
              <input type="email" name="email" placeholder="Email" />
              <input type="file" name="resume" />
              <textarea name="q1" required placeholder="Essay 1"></textarea>
              <textarea name="q2" required placeholder="Essay 2"></textarea>
              <textarea name="q3" required placeholder="Essay 3"></textarea>
              <button type="submit">Submit Application</button>
            </form>
          </main>
        </body></html>
        """,
        encoding="utf-8",
    )
    url = f"file://{html.resolve()}"

    cfg_strict = AppConfig.model_validate({"automation": {"max_free_text_questions": 2}})
    monkeypatch.setattr("src.apply.detector.load_config", lambda: cfg_strict)
    status1, reason1 = await classify(url)
    assert status1 == "COMPLEX"
    assert "max_free_text_questions" in reason1

    cfg_lenient = AppConfig.model_validate({"automation": {"max_free_text_questions": 3}})
    monkeypatch.setattr("src.apply.detector.load_config", lambda: cfg_lenient)
    status2, _reason2 = await classify(url)
    assert status2 == "AUTO_SIMPLE"


@pytest.mark.asyncio
async def test_detector_flags_captcha_and_apply_graph_sets_human_gate(monkeypatch, tmp_path):
    html = tmp_path / "cloudflare_gate.html"
    html.write_text(
        """
        <html><body>
          <main>
            <h1>Performing security verification</h1>
            <p>Please verify you are human to continue.</p>
            <div class="cf-turnstile" style="width:300px;height:65px;">Turnstile</div>
          </main>
        </body></html>
        """,
        encoding="utf-8",
    )
    url = f"file://{html.resolve()}"
    status, reason = await classify(url)
    assert status == "CAPTCHA"
    assert "human handoff" in reason.lower()

    # Verify ApplyAgentGraph propagates human_gate=True on aggregator/CAPTCHA escalations
    monkeypatch.delenv("RELENTLESS_TEST_LIVE_DB", raising=False)
    cfg_prep = AppConfig.model_validate({"automation": {"apply_mode": "PREPARE_ONLY"}})
    monkeypatch.setattr("src.apply.runner.load_config", lambda: cfg_prep)
    dummy_pdf = tmp_path / "resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 test")

    gres = await run_apply_agent_graph(
        {
            "id": 505,
            "company": "LinkedInCo",
            "title": "AI Engineer",
            "apply_url": "https://www.linkedin.com/jobs/view/99999/",
            "tailored_pdf_path": str(dummy_pdf),
            "human_action": "APPLIED",
        },
        action="PREPARE",
        use_interrupt=False,
    )
    assert gres["status"] == "ESCALATED"
    assert gres["router_path"] == "MANUAL_APPLY"
    assert gres["human_gate"] is True


# ==============================================================================
# 4. Web API v4 Routes: Models Catalog, Location Policy Tester, Presets, Skip Reasons, Evals
# ==============================================================================
@pytest.mark.asyncio
async def test_web_api_v4_models_location_tester_preset_and_skip_reason(monkeypatch):
    async with _client() as client:
        # 1. Model Catalog
        r_cat = await client.get("/api/models/catalog")
        assert r_cat.status_code == 200
        cat = r_cat.json()
        assert cat["ok"] is True
        assert any(m["id"] == "gpt-4o" for m in cat["chat_models"]["openai"])
        assert any(m["id"] == "text-embedding-3-small" for m in cat["embedding_models"]["openai"])
        assert any(m["id"] == "typesafe/jev-1.14" for m in cat["triage_models"]["openrouter"])

        # 2. Live Location Policy Tester (Remote-only + Hybrid OR + Indiana IN vs India)
        remote_only_policy = {
            "operator": "OR",
            "candidate_country": "india",
            "reject_countries": ["us", "uk", "eu"],
            "check_jd_geo_blockers": True,
            "rules": [
                {
                    "name": "remote_anywhere",
                    "work_modes": ["remote"],
                    "allowed_regions": ["anywhere", "worldwide", "global", "india", "apac"],
                    "allow_unspecified_remote": True,
                }
            ],
        }

        r_ok = await client.post(
            "/api/location-policy/test",
            json={
                "title": "AI Engineer",
                "location": "Remote (Worldwide)",
                "location_policy": remote_only_policy,
            },
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["accepted"] is True
        assert r_ok.json()["facts"]["work_mode"] == "remote"

        r_in_us = await client.post(
            "/api/location-policy/test",
            json={
                "title": "ML Engineer",
                "location": "Indianapolis, IN",
                "location_policy": remote_only_policy,
            },
        )
        assert r_in_us.status_code == 200
        assert r_in_us.json()["accepted"] is False
        assert "us" in r_in_us.json()["facts"]["countries"]
        assert "india" not in r_in_us.json()["facts"]["countries"]

        r_us_rem = await client.post(
            "/api/location-policy/test",
            json={
                "title": "ML Engineer",
                "location": "San Francisco, CA or Remote, US",
                "location_policy": remote_only_policy,
            },
        )
        assert r_us_rem.status_code == 200
        assert r_us_rem.json()["accepted"] is False

        # 3. Preset application endpoint
        monkeypatch.setattr(
            "src.hitl.service.async_apply_preset_and_save",
            lambda preset: _async_val({"ok": True, "preset": preset, "config": {"automation": {"apply_mode": "CONFIRM_SUBMIT"}}}),
        )
        r_pre = await client.post("/api/settings/preset", json={"preset": "balanced"})
        assert r_pre.status_code == 200
        assert r_pre.json()["preset"] == "balanced"

        # 4. Structured skip reason with learning gate
        recorded = {}

        async def _fake_record(job_id, action, actor_source="web", reason_category=None, reason_note=None, **kwargs):
            from src.knowledge.distiller import should_learn_from_skip

            learn, why = should_learn_from_skip(action, reason_category, reason_note)
            recorded.update({"job_id": job_id, "reason_category": reason_category, "learn": learn})
            return {
                "ok": True,
                "job_id": job_id,
                "action": action,
                "reason_category": reason_category,
                "reason_note": reason_note,
                "learned_from_action": learn,
                "learning_reason": why,
            }

        monkeypatch.setattr("src.hitl.service.record_job_action", _fake_record)

        r_exp = await client.post(
            "/api/jobs/91/action",
            json={"action": "SKIP_BAD_FIT", "reason_category": "EXPIRED_OR_CLOSED", "reason_note": "404 dead link"},
        )
        assert r_exp.status_code == 200
        assert r_exp.json()["learned_from_action"] is False

        r_skill = await client.post(
            "/api/jobs/92/action",
            json={"action": "SKIP_BAD_FIT", "reason_category": "BAD_MATCH_EXPERIENCE", "reason_note": "Needs 10+ yrs"},
        )
        assert r_skill.status_code == 200
        assert r_skill.json()["learned_from_action"] is True

        # 5. Experiments & Calibration endpoints
        monkeypatch.setattr("src.hitl.service.async_list_experiments", lambda: _async_val([{"id": 1, "status": "RUNNING"}]))
        monkeypatch.setattr("src.hitl.service.async_start_experiment", lambda p: _async_val({"id": 2, "status": "RUNNING", "params": p}))
        monkeypatch.setattr("src.hitl.service.async_complete_experiment", lambda eid, accept=None: _async_val({"id": eid, "status": "ACCEPTED"}))
        monkeypatch.setattr("src.hitl.service.async_trigger_calibration", lambda min_n=30, min_per_class=10: _async_val({"ok": False, "activated": False}))

        assert (await client.get("/api/eval/experiments")).json()["experiments"][0]["id"] == 1
        assert (await client.post("/api/eval/experiments/start", json={"note": "v4 test"})).json()["experiment"]["id"] == 2
        assert (await client.post("/api/eval/experiments/2/complete", json={"accept": True})).json()["experiment"]["status"] == "ACCEPTED"
        assert (await client.post("/api/eval/calibrate", json={})).json()["activated"] is False


async def _async_val(v):
    return v


# ==============================================================================
# 5. Telegram Structured Skip Keyboard Compatibility
# ==============================================================================
def test_telegram_keyboards_preserve_legacy_and_add_skip_reason_menu():
    main_kb = build_inline_keyboard(77)
    rows = main_kb["inline_keyboard"]
    assert len(rows) == 2
    assert rows[0][0]["callback_data"] == "interest:77"
    assert rows[0][1]["callback_data"] == "act:SKIP_BAD_FIT:77"
    assert any(btn["callback_data"] == "skip_menu:77" for btn in rows[1])

    skip_kb = build_skip_reason_keyboard(77)["inline_keyboard"]
    callbacks = [btn["callback_data"] for row in skip_kb for btn in row]
    assert "skipr:EXPIRED_OR_CLOSED:77" in callbacks
    assert "skipr:BAD_MATCH_EXPERIENCE:77" in callbacks
    assert "skipr:BAD_MATCH_SKILLS:77" in callbacks
    assert "skipr:BAD_MATCH_LOCATION:77" in callbacks
    assert "skipr:SCAM_OR_BAD_COMPANY:77" in callbacks
    assert "skip_note:77" in callbacks
    assert "skip_cancel:77" in callbacks


# ==============================================================================
# 6. Web Frontend ES Module Syntax & DOM Integrity Check (Real Chromium)
# ==============================================================================
@pytest.mark.asyncio
async def test_web_app_js_parses_cleanly_in_chromium():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    index_html = (root / "web" / "index.html").read_text(encoding="utf-8")
    app_js = (root / "web" / "app.js").read_text(encoding="utf-8")
    styles_css = (root / "web" / "styles.css").read_text(encoding="utf-8")

    # Verify key Relentless v4 Web UI DOM elements exist
    for dom_id in (
        "ec-mod-tailor-provider",
        "ec-mod-embed-provider",
        "ec-lp-operator",
        "btn-ec-test-loc",
        "skip-modal",
        "skip-reason-select",
        "btn-eval-start-exp",
        "btn-eval-calibrate",
    ):
        assert dom_id in index_html, f"Missing v4 DOM element: {dom_id}"

    page_errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        async def _route_handler(route):
            url = route.request.url
            if url.endswith("/app.js"):
                await route.fulfill(status=200, content_type="application/javascript", body=app_js)
            elif url.endswith("/styles.css"):
                await route.fulfill(status=200, content_type="text/css", body=styles_css)
            elif "/api/auth/status" in url:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"authenticated":false,"mode":"unconfigured","auth_required":true}',
                )
            elif "/api/" in url:
                await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            else:
                await route.fulfill(status=200, content_type="text/html", body=index_html)

        await page.route("http://relentless.test/**", _route_handler)
        await page.goto("http://relentless.test/", wait_until="networkidle")
        await browser.close()

    assert page_errors == [], f"JS runtime/syntax errors in web/app.js: {page_errors}"


def test_pause_blocks_submit_and_background_prepare_but_allows_explicit_human_preview(monkeypatch):
    from src.apply.runner import _guard

    cfg = AppConfig.model_validate({"automation": {"apply_mode": "CONFIRM_SUBMIT"}})
    monkeypatch.setattr("src.apply.runner.load_config", lambda: cfg)
    monkeypatch.setattr("src.apply.runner._is_paused", lambda: True)

    # Background auto-prepare (no human_action) MUST still be blocked when paused
    bg_ok, bg_reason = _guard(
        {"id": 1, "company": "Twilio", "jev_fit_prob": 0.9, "jev_fit_tier": "strong_fit"},
        "PREPARE",
    )
    assert bg_ok is False
    assert "paused" in bg_reason

    # Live SUBMIT MUST ALWAYS be blocked when paused
    sub_ok, sub_reason = _guard(
        {"id": 1, "company": "Twilio", "human_action": "APPLIED", "workflow_stage": "READY_FOR_REVIEW"},
        "SUBMIT",
    )
    assert sub_ok is False
    assert "paused" in sub_reason

    # Explicit human-triggered PREPARE preview (user clicked Interested / Re-check Website) IS allowed!
    human_ok, _ = _guard(
        {"id": 1, "company": "Twilio", "human_action": "APPLIED", "workflow_stage": "PROCESSING"},
        "PREPARE",
    )
    assert human_ok is True


@pytest.mark.asyncio
async def test_react_select_combobox_extraction_and_fill(tmp_path):
    """Verifies Greenhouse/Ashby React-Select role='combobox' inputs are detected and populated cleanly."""
    from src.apply.llm_form_agent import smart_fill_form

    html = tmp_path / "react_select_greenhouse.html"
    html.write_text(
        """
        <html><body>
          <main>
            <form>
              <label for="first_name">First Name*</label>
              <input id="first_name" type="text" required />
              <label for="country">Country*</label>
              <div class="select__control">
                <div class="select__value-container">
                  <span id="country-chosen" class="select__single-value"></span>
                  <input id="country" type="text" role="combobox" aria-autocomplete="list" class="select__input" required />
                </div>
                <div id="country-menu" style="display:none;">
                  <div role="option" class="select__option" onclick="document.getElementById('country-chosen').innerText='India'; document.getElementById('country-menu').style.display='none';">India</div>
                  <div role="option" class="select__option" onclick="document.getElementById('country-chosen').innerText='United States'; document.getElementById('country-menu').style.display='none';">United States</div>
                </div>
              </div>
              <fieldset>
                <legend>By clicking Acknowledge, you consent to our privacy policy</legend>
                <input id="consent_1" name="consent[]" type="checkbox" required />
                <label for="consent_1">Acknowledge</label>
              </fieldset>
              <button type="submit">Submit Application</button>
            </form>
            <script>
              document.getElementById('country').addEventListener('input', () => {
                document.getElementById('country-menu').style.display = 'block';
              });
            </script>
          </main>
        </body></html>
        """,
        encoding="utf-8",
    )
    url = f"file://{html.resolve()}"
    profile = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "application_defaults": {"first_name": "Ada", "country": "India"},
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)
        schema = await extract_form_schema(page)
        country_f = next(f for f in schema if f["id"] == "country")
        assert country_f["type"] == "combobox"
        assert "Country" in country_f["label"]

        consent_f = next(f for f in schema if f["id"] == "consent_1")
        assert "privacy policy" in consent_f["label"].lower()

        filled = await smart_fill_form(page, {"id": 1, "company": "Twilio", "title": "MLE"}, profile)
        assert filled.get("country") == "India"
        assert await page.inner_text("#country-chosen") == "India"
        assert await page.is_checked("#consent_1") is True
        await browser.close()


@pytest.mark.asyncio
async def test_recheck_website_skip_tailor_does_not_call_llm(monkeypatch, tmp_path):
    """Verifies tapping 'Re-check Website' reuses existing tailored PDF without re-tailoring."""
    from contextlib import contextmanager

    from src.hitl import telegram_bot as tg

    dummy_pdf = tmp_path / "existing.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 existing custom resume")

    tailor_called = False

    async def _fail_if_tailored(*args, **kwargs):
        nonlocal tailor_called
        tailor_called = True
        raise AssertionError("Re-check Website must NOT re-tailor an already tailored resume!")

    async def _fake_apply_graph(job, action="PREPARE", **kwargs):
        return {"status": "PREPARED", "feasible": True, "screenshots": [], "router_path": "AUTO_SIMPLE"}

    async def _fake_upsert(**kwargs):
        return 1234

    class _FakeCur:
        def execute(self, sql, params=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    @contextmanager
    def _fake_conn():
        yield _FakeConn()

    monkeypatch.setattr(tg, "tailor_resume_for_job", _fail_if_tailored)
    monkeypatch.setattr("src.orchestrator.graph.run_apply_agent_graph", _fake_apply_graph)
    monkeypatch.setattr(tg, "upsert_job_rich_card", _fake_upsert)
    monkeypatch.setattr(tg, "get_connection", _fake_conn)
    monkeypatch.setattr(tg, "create_apply_confirmation", lambda *a, **k: ("mock-token", None))

    job_row = {
        "id": 3983,
        "company": "Twilio",
        "title": "ML Engineer",
        "apply_url": "https://job-boards.greenhouse.io/twilio/jobs/8258587",
        "tailored_pdf_path": str(dummy_pdf),
        "tailored_pdf_sha256": "abc123",
        "tailored_diff": "Existing custom diff",
        "tailored_resume_json": {"tailored_summary": "Custom edited summary"},
    }
    await tg._process_job_interest(job_row, chat_id=0, skip_tailor=True)
    assert tailor_called is False


def test_git_like_sync_blocks_overwriting_newer_db_and_pull_updates_local(monkeypatch, tmp_path):
    """Verifies safe sync conflict protection and pull_db_to_local()."""
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta
    import json

    from src import settings_store as ss

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("automation:\n  apply_mode: MANUAL_ONLY\n", encoding="utf-8")
    prof_path = tmp_path / "master_profile.json"
    prof_path.write_text(json.dumps({"name": "Local Old"}), encoding="utf-8")

    monkeypatch.setattr(ss, "LOCAL_CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(ss, "LOCAL_PROFILE_PATH", str(prof_path))

    future_ts = datetime.now(UTC) + timedelta(hours=1)
    executed = []

    class FakeCur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchall(self):
            return [
                {"key": "config_yaml", "value": {"automation": {"apply_mode": "CONFIRM_SUBMIT"}}, "updated_at": future_ts},
                {"key": "master_profile", "value": {"name": "Cloud Updated"}, "updated_at": future_ts},
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    @contextmanager
    def fake_conn():
        yield FakeConn()

    monkeypatch.setattr(ss, "get_connection", fake_conn)

    # 1. Safe sync MUST skip both because DB is newer!
    res = ss.sync_local_to_db(force=False)
    assert set(res["skipped"]) == {"master_profile", "config_yaml"}
    assert not any("INSERT INTO app_settings" in s for s in executed)

    # 2. Pull downloads Cloud Updated values cleanly to local files!
    pulled = ss.pull_db_to_local()
    assert pulled == {"master_profile": True, "config_yaml": True}
    assert json.loads(prof_path.read_text(encoding="utf-8"))["name"] == "Cloud Updated"
    assert "CONFIRM_SUBMIT" in cfg_path.read_text(encoding="utf-8")



