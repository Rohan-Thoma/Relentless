from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from psycopg.types.json import Jsonb

from src.apply.autofill import fill
from src.apply.detector import _analyze_page_dom, classify, detect_aggregator_shell
from src.apply.llm_form_agent import dismiss_popups_and_open_form, plan_vision_navigation_step
from src.config import load_config
from src.db import consume_apply_confirmation, get_connection
from src.settings_store import get_master_profile
from src.tailor.resume_tailor import ensure_tailored_pdf

logger = logging.getLogger(__name__)

_BROWSER_SEM = asyncio.Semaphore(int(os.getenv("PLAYWRIGHT_CONCURRENCY", "2")))


def _load_automation() -> dict[str, Any]:
    """Single source of truth via load_config() (eliminates stale manual YAML/DB drift)."""
    cfg = load_config()
    auto = cfg.automation.model_dump()
    auto["apply_mode"] = cfg.automation.normalized_apply_mode()
    return auto


def _is_paused() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return False
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'automation_paused_until';")
            row = cur.fetchone()
            if row and row.get("value"):
                until = row["value"]
                if isinstance(until, str):
                    until = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if until:
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=UTC)
                    if datetime.now(UTC) < until:
                        return True
    except Exception:
        pass
    return False


def _guard(job: dict[str, Any], action_kind: str = "PREPARE") -> tuple[bool, str]:
    """Strictly enforces pause state, apply_mode, daily cap, company cooldown, first-company confirm, and fit rules."""
    auto = _load_automation()
    mode = auto.get("apply_mode", "PREPARE_ONLY")

    if mode == "MANUAL_ONLY":
        return False, "apply_mode is MANUAL_ONLY (automated browser execution disabled)"

    if action_kind == "SUBMIT" and mode != "CONFIRM_SUBMIT":
        return (
            False,
            f"apply_mode is {mode} (submissions blocked; switch to CONFIRM_SUBMIT to submit)",
        )

    is_human_interest = job.get("human_action") in ("APPLIED", "INTERESTED") or job.get("workflow_stage") in (
        "PROCESSING",
        "EDITING",
        "READY_FOR_REVIEW",
        "SUBMITTING",
    )

    # Automation pause blocks background auto-prepare and ALL live submissions,
    # while still allowing a human who explicitly tapped "Interested / Re-check Website" to inspect & preview the form.
    if _is_paused() and (action_kind == "SUBMIT" or not is_human_interest):
        return False, "automation paused (resume via Web UI -> 06 // Vault or Topbar Pause button)"

    daily_cap = int(auto.get("daily_cap", 5))
    cooldown = int(auto.get("per_company_cooldown_hours", 72))

    if not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM apply_events WHERE started_at > NOW() - INTERVAL '1 day' AND status = 'SUBMITTED';"
                )
                if int(cur.fetchone()["c"]) >= daily_cap:
                    return False, f"daily cap reached ({daily_cap}/day)"

                cur.execute(
                    "SELECT MAX(started_at) AS last FROM apply_events WHERE job_id IN (SELECT id FROM jobs WHERE LOWER(company) = LOWER(%s)) AND status = 'SUBMITTED';",
                    (job["company"],),
                )
                row = cur.fetchone()
                if row and row.get("last"):
                    last = row["last"]
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=UTC)
                    if datetime.now(UTC) - last < timedelta(hours=cooldown):
                        return False, f"company cooldown active ({cooldown}h)"
        except Exception as e:
            logger.debug("DB guard query skipped: %s", e)

    if not is_human_interest and action_kind == "PREPARE":
        if float(job.get("jev_fit_prob") or 0.0) < float(auto.get("min_fit_prob", 0.65)):
            return False, "fit prob below threshold"
        allowed_tiers = auto.get("allowed_tiers") or []
        if allowed_tiers and job.get("jev_fit_tier") not in allowed_tiers:
            return False, "tier not allowed"

    return True, "ok"


def _domain_allowed(url: str) -> bool:
    auto = _load_automation()
    allowed = auto.get("allowed_apply_domains") or []
    host = (urlparse(url).hostname or "").lower()
    if not allowed:
        return True
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


async def _collect_non_nav_buttons(page) -> list[dict[str, Any]]:
    try:
        return await page.evaluate(
            """() => {
            const isVisible = (el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            return Array.from(document.querySelectorAll('button, a, [role="button"]'))
                .filter(el => isVisible(el) && !el.closest('header, nav, footer'))
                .slice(0, 30)
                .map((el, idx) => ({
                    idx,
                    tag: el.tagName.toLowerCase(),
                    id: el.id || '',
                    text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80)
                }));
        }"""
        )
    except Exception:
        return []


async def _run_prepare_browser_steps(
    job: dict[str, Any],
    evidence_dir: Path,
    headless: bool = True,
    max_nav_steps: int = 3,
) -> dict[str, Any]:
    """Multi-step Vision+DOM browser preparation loop (never clicks final submit; pauses cleanly on CAPTCHA/Login)."""
    screenshots: list[str] = []
    fields_filled: dict[str, str] = {}
    router_path = "NEEDS_REVIEW"
    blocker: str | None = None
    status = "PREPARED"
    error: str | None = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        await page.goto(job["apply_url"], timeout=30000, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        target = await dismiss_popups_and_open_form(page)
        router_path, rreason = await _analyze_page_dom(target)

        # Multi-step Vision+DOM navigation loop if landing page hasn't revealed the form yet
        for step in range(1, max_nav_steps + 1):
            if router_path in ("AUTO_SIMPLE", "CAPTCHA", "NEEDS_LOGIN", "COMPLEX"):
                break
            step_shot = str(evidence_dir / f"step_nav_{step}.png")
            await page.screenshot(path=step_shot)
            screenshots.append(step_shot)
            btns = await _collect_non_nav_buttons(page)
            decision = await plan_vision_navigation_step(step_shot, btns, job)
            act = str(decision.get("action") or "STOP").upper()
            if act == "HUMAN_GATE":
                router_path = "HUMAN_GATE"
                rreason = decision.get("reason") or "CAPTCHA or login gate detected"
                break
            if act == "CLICK" and decision.get("selector"):
                try:
                    el = await page.query_selector(decision["selector"])
                    if el and await el.is_visible():
                        await el.click(timeout=5000)
                        await page.wait_for_timeout(1500)
                        target = await dismiss_popups_and_open_form(page)
                        router_path, rreason = await _analyze_page_dom(target)
                        continue
                except Exception:
                    pass
            break

        feasible = router_path == "AUTO_SIMPLE"
        blocker = None if feasible else (rreason or router_path)

        if not feasible:
            sp_esc = str(evidence_dir / "step_escalated.png")
            await page.screenshot(path=sp_esc)
            screenshots.append(sp_esc)
            await browser.close()
            return {
                "status": "ESCALATED",
                "router_path": router_path,
                "error": blocker,
                "feasible": False,
                "fields_filled": {},
                "screenshots": screenshots,
                "human_gate": router_path in ("CAPTCHA", "NEEDS_LOGIN", "HUMAN_GATE"),
            }

        sp0 = str(evidence_dir / "step_0.png")
        await page.screenshot(path=sp0)
        screenshots.append(sp0)

        profile = get_master_profile()
        fields_filled = await fill(page, job, profile)

        sp_filled = str(evidence_dir / "step_filled.png")
        await page.screenshot(path=sp_filled)
        screenshots.append(sp_filled)

        await browser.close()

    return {
        "status": status,
        "router_path": router_path,
        "error": error,
        "feasible": True,
        "fields_filled": fields_filled,
        "screenshots": screenshots,
        "human_gate": False,
    }


async def prepare_application(job: dict[str, Any] | int) -> dict[str, Any]:
    """Prepare an application via LangGraph ApplyAgentGraph (prefill + evidence). Never submits."""
    job_id = job.get("id") if isinstance(job, dict) else job
    if job_id and not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        def _fetch_fresh():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
                return cur.fetchone()

        try:
            fresh = await asyncio.to_thread(_fetch_fresh)
            if fresh:
                fresh_dict = dict(fresh)
                if isinstance(job, dict):
                    for k in ("tailored_pdf_path", "tailored_pdf_sha256", "pdf_path", "tailored_resume_json"):
                        if job.get(k):
                            fresh_dict[k] = job[k]
                job = fresh_dict
        except Exception:
            pass

    if not isinstance(job, dict):
        return {"status": "ABORTED", "error": "job not found", "feasible": False}

    # 1. Enforce apply_mode & _guard() BEFORE doing browser work
    ok, reason = await asyncio.to_thread(_guard, job, "PREPARE")
    if not ok:
        return {"status": "ABORTED", "error": reason, "feasible": False, "screenshots": []}

    # 2. Ensure tailored PDF exists on disk
    pdf_path = await ensure_tailored_pdf(job)
    if pdf_path:
        job["tailored_pdf_path"] = pdf_path
    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    pdf_sha = job.get("tailored_pdf_sha256")
    if pdf_path and Path(pdf_path).exists() and not pdf_sha:
        pdf_sha = await asyncio.to_thread(_sha256_file, pdf_path)
    if not pdf_path or not Path(pdf_path).exists():
        return {"status": "ABORTED", "error": "missing tailored PDF", "feasible": False}

    # 3. Fast pre-flight aggregator shell detection (never waste Chromium on LinkedIn/Naukri/Workday!)
    shell = detect_aggregator_shell(job.get("apply_url") or "")
    if shell is not None:
        r_path, blocker = shell
        if job.get("id") and not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
            def _save_manual():
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET apply_feasible=FALSE, apply_blocker=%s, apply_state='MANUAL_APPLY' WHERE id=%s;",
                        (blocker, job["id"]),
                    )
                    if cur.rowcount:
                        cur.execute(
                            "INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, started_at, finished_at) VALUES (%s,%s,%s,%s,%s,%s,NOW(),NOW());",
                            (job["id"], 1, r_path, "PREPARE", "ESCALATED", blocker),
                        )
                    conn.commit()
            await asyncio.to_thread(_save_manual)
        return {
            "status": "ESCALATED",
            "router_path": r_path,
            "error": blocker,
            "feasible": False,
            "screenshots": [],
            "human_gate": True,
        }

    # 4. Domain allowlist check
    if not _domain_allowed(job.get("apply_url") or ""):
        if not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
            def _freeze_domain():
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET apply_feasible=FALSE, apply_blocker='domain not allowed', apply_state='NEEDS_REVIEW' WHERE id=%s;",
                        (job["id"],),
                    )
                    conn.commit()
            await asyncio.to_thread(_freeze_domain)
        return {"status": "ABORTED", "error": "domain not allowed", "feasible": False}

    evidence_dir = Path("output") / "apply_evidence" / str(job["id"])
    evidence_dir.mkdir(parents=True, exist_ok=True)

    async with _BROWSER_SEM:
        try:
            res = await _run_prepare_browser_steps(job, evidence_dir, headless=True)
        except Exception as e:
            res = {
                "status": "FAILED",
                "router_path": "FAILED",
                "error": str(e),
                "feasible": False,
                "fields_filled": {},
                "screenshots": [],
                "human_gate": False,
            }
            logger.warning("Prepare failed for %s: %s", job.get("company"), e)

    if not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        def _persist():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET apply_feasible=%s, apply_blocker=%s, apply_status=%s, apply_state=%s, apply_last_at=NOW(), tailored_pdf_path=%s, tailored_pdf_sha256=%s WHERE id=%s;",
                    (
                        res["feasible"],
                        res.get("error"),
                        res["status"],
                        res["status"] if res["feasible"] else "NEEDS_REVIEW",
                        pdf_path,
                        pdf_sha,
                        job["id"],
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, fields_filled, screenshots, started_at, finished_at, evidence_path)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s);
                    """,
                    (
                        job["id"],
                        1,
                        res["router_path"],
                        "PREPARE",
                        res["status"],
                        res.get("error"),
                        Jsonb(res.get("fields_filled") or {}),
                        res.get("screenshots") or [],
                        str(evidence_dir),
                    ),
                )
                conn.commit()
        await asyncio.to_thread(_persist)

    if not res["feasible"]:
        logger.info(
            "Prepare escalated for %s (%s) — %s",
            job.get("company"),
            res.get("error"),
            job.get("apply_url"),
        )
    return res


async def submit_application(job: dict[str, Any], confirmation_token: str) -> dict[str, Any]:
    """Submit ONLY when apply_mode == CONFIRM_SUBMIT, guards pass, AND confirmation token is valid."""
    # 1. STRICT apply_mode & guard enforcement BEFORE consuming token or opening browser!
    ok, guard_reason = await asyncio.to_thread(_guard, job, "SUBMIT")
    if not ok:
        mode = _load_automation().get("apply_mode", "PREPARE_ONLY")
        status_code = "DRY_RUN_BLOCKED" if mode in ("PREPARE_ONLY", "MANUAL_ONLY") else "REJECTED"
        return {"status": status_code, "error": guard_reason}

    # 2. Fast check: never attempt Playwright submission on aggregator shells (LinkedIn/Naukri/Workday)
    shell = detect_aggregator_shell(job.get("apply_url") or "")
    if shell is not None:
        return {"status": "ESCALATED", "router_path": shell[0], "error": shell[1]}

    # 3. Consume single-use SHA-256 job-bound confirmation token
    row = await asyncio.to_thread(consume_apply_confirmation, confirmation_token, job_id=job["id"], kind="SUBMIT")
    if not row:
        return {"status": "REJECTED", "error": "invalid/expired/used token"}
    payload = row.get("payload") or {}

    pdf_path = await ensure_tailored_pdf(job)
    if pdf_path:
        job["tailored_pdf_path"] = pdf_path

    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    pdf_sha = job.get("tailored_pdf_sha256")
    if pdf_path and Path(pdf_path).exists() and not pdf_sha:
        pdf_sha = await asyncio.to_thread(_sha256_file, pdf_path)

    if payload.get("pdf_sha256") and payload.get("pdf_sha256") != pdf_sha:
        return {"status": "REJECTED", "error": "PDF hash mismatch"}
    if payload.get("apply_url") and payload.get("apply_url") != job["apply_url"]:
        return {"status": "REJECTED", "error": "apply URL mismatch"}
    if not _domain_allowed(job["apply_url"]):
        return {"status": "REJECTED", "error": "domain not allowed"}

    evidence_dir = Path("output") / "apply_evidence" / str(job["id"])
    evidence_dir.mkdir(parents=True, exist_ok=True)
    router_path, rreason = await classify(job["apply_url"])
    if router_path != "AUTO_SIMPLE":
        return {"status": "ESCALATED", "router_path": router_path, "error": rreason}

    screenshots: list[str] = []
    fields_filled: dict[str, str] = {}
    status = "SUBMITTED"
    error: str | None = None
    verification = "unverified"
    final_url: str | None = None

    async with _BROWSER_SEM:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(job["apply_url"], timeout=30000, wait_until="domcontentloaded")
                profile = get_master_profile()
                fields_filled = await fill(page, job, profile)

                submit_selectors = [
                    "form button[type=submit]",
                    "form input[type=submit]",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "input[value*='Submit' i]",
                ]
                submit = None
                for sel in submit_selectors:
                    try:
                        cand = await page.query_selector(sel)
                        if cand and await cand.is_visible():
                            submit = cand
                            break
                    except Exception:
                        pass

                if not submit:
                    for frame in page.frames:
                        if frame == page.main_frame:
                            continue
                        for sel in submit_selectors:
                            try:
                                cand = await frame.query_selector(sel)
                                if cand and await cand.is_visible():
                                    submit = cand
                                    break
                            except Exception:
                                pass
                        if submit:
                            break

                if not submit:
                    status = "FAILED"
                    error = "no application submit button found"
                else:
                    pre_submit_sp = str(evidence_dir / "step_before_submit.png")
                    await page.screenshot(path=pre_submit_sp)
                    screenshots.append(pre_submit_sp)

                    click_ok = False
                    for attempt in range(3):
                        try:
                            await submit.scroll_into_view_if_needed(timeout=4000)
                            await submit.click(timeout=10000)
                            click_ok = True
                            break
                        except Exception:
                            try:
                                await submit.evaluate("el => el.click()")
                                click_ok = True
                                break
                            except Exception:
                                await page.wait_for_timeout(500 + attempt * 300)

                    if not click_ok:
                        status = "FAILED"
                        error = "submit button click failed after retries"
                    else:
                        await page.wait_for_timeout(3000)
                        sp = str(evidence_dir / "step_submitted.png")
                        await page.screenshot(path=sp)
                        screenshots.append(sp)
                        final_url = page.url

                        # Check visible rendered text & visible blocking challenge frames ONLY
                        # (never false-positive on Greenhouse's invisible reCAPTCHA Enterprise script in HTML!).
                        post_dom = await page.evaluate(
                            """() => {
                            const isVisible = (el) => {
                                if (!el) return false;
                                const st = window.getComputedStyle(el);
                                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                                const r = el.getBoundingClientRect();
                                return r.width > 100 && r.height > 40;
                            };
                            const visibleText = (document.body ? document.body.innerText : '').toLowerCase();
                            const blockingSels = [
                                'iframe[src*="recaptcha/api2/bframe"]',
                                'iframe[src*="recaptcha/enterprise/bframe"]',
                                'iframe[src*="hcaptcha.com"]',
                                'iframe[src*="challenges.cloudflare.com"]',
                                '.cf-turnstile',
                                '.h-captcha'
                            ];
                            let visibleCaptcha = false;
                            for (const sel of blockingSels) {
                                for (const el of document.querySelectorAll(sel)) {
                                    if (isVisible(el)) visibleCaptcha = true;
                                }
                            }
                            return { visibleText, visibleCaptcha };
                        }"""
                        )
                        visible_text = post_dom.get("visibleText") or ""
                        has_captcha = bool(post_dom.get("visibleCaptcha")) or any(
                            m in visible_text for m in ("verify you are human", "performing security verification", "complete the captcha")
                        )
                        has_login = any(m in visible_text for m in ("join or sign in", "sign in to apply", "create an account"))
                        has_confirm = any(
                            m in visible_text
                            for m in (
                                "thank you for applying",
                                "thank you",
                                "application received",
                                "application submitted",
                                "application has been submitted",
                                "successfully submitted",
                                "we have received your application",
                                "confirmation",
                            )
                        )
                        url_confirm = any(m in (final_url or "").lower() for m in ("thank", "confirm", "success", "submitted"))

                        if has_confirm or url_confirm:
                            verification = "verified"
                            status = "SUBMITTED"
                        elif has_captcha:
                            verification = "captcha"
                            status = "NEEDS_REVIEW"
                            error = "CAPTCHA challenge appeared after submit (human handoff required)"
                        elif has_login:
                            verification = "login_required"
                            status = "NEEDS_REVIEW"
                            error = "login wall appeared after submit"
                        else:
                            verification = "ambiguous"
                            status = "NEEDS_REVIEW"
                            error = "submission could not be positively verified — inspect step_submitted.png"
                await browser.close()
        except Exception as e:
            status = "FAILED"
            error = str(e)
            logger.warning("Submit failed for %s: %s", job.get("company"), e)

    if status == "SUBMITTED" and verification != "verified":
        status = "NEEDS_REVIEW"

    if not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        def _persist_submit_results():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, fields_filled, screenshots, confirmation_id, pdf_sha256, confirmed_by, verification_status, evidence_path, started_at, finished_at, submitted_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s);
                    """,
                    (
                        job["id"],
                        1,
                        router_path,
                        "SUBMIT",
                        status,
                        error,
                        Jsonb(fields_filled),
                        screenshots,
                        row["id"],
                        pdf_sha,
                        None,
                        verification,
                        str(evidence_dir),
                        datetime.now(UTC) if status == "SUBMITTED" else None,
                    ),
                )
                cur.execute(
                    "UPDATE jobs SET apply_status=%s, apply_state=%s, apply_last_at=NOW() WHERE id=%s;",
                    (status, status, job["id"]),
                )
                conn.commit()
        await asyncio.to_thread(_persist_submit_results)

    if status != "SUBMITTED":
        logger.warning("Apply not confirmed for %s: %s %s", job.get("company"), status, error or "")
    return {
        "status": status,
        "router_path": router_path,
        "error": error,
        "verification": verification,
        "screenshots": screenshots,
    }


async def attempt_apply(job, mode=None):
    """Backward-compatible wrapper: never submits."""
    return await prepare_application(job)


async def auto_apply_cycle(limit=3):
    cfg = load_config()
    if cfg.automation.normalized_apply_mode() == "MANUAL_ONLY":
        return
    def _fetch_candidates():
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, company, title, apply_url, jev_fit_prob, jev_fit_tier, tailored_pdf_path, tailored_pdf_sha256, tailored_resume_json
                FROM jobs
                WHERE triage_passed = TRUE AND human_action IS NULL
                  AND (apply_status IS NULL OR apply_status IN ('NOT_APPLIED','PREPARED'))
                ORDER BY jev_fit_prob DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return cur.fetchall()

    jobs = await asyncio.to_thread(_fetch_candidates)
    for j in jobs:
        await prepare_application(dict(j))


async def prepare_apply_cycle(limit=3):
    await auto_apply_cycle(limit=limit)
