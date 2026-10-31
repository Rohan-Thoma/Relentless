import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from psycopg.types.json import Jsonb
import yaml

from src.apply.autofill import fill
from src.apply.detector import _analyze_page_dom, classify
from src.apply.llm_form_agent import dismiss_popups_and_open_form
from src.db import consume_apply_confirmation, get_connection
from src.notify import alert
from src.settings_store import get_master_profile
from src.tailor.resume_tailor import ensure_tailored_pdf

logger = logging.getLogger(__name__)

CONFIG_PATH = "config/config.yaml"
_BROWSER_SEM = asyncio.Semaphore(int(os.getenv("PLAYWRIGHT_CONCURRENCY", "2")))


def _load_automation():
    auto = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        auto = cfg.get("automation", {}) or {}
    except Exception:
        auto = {}
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'config_yaml';")
            row = cur.fetchone()
            if row and row.get("value"):
                auto = row["value"].get("automation") or auto
    except Exception:
        pass
    return auto


def _is_paused():
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'automation_paused_until';")
            row = cur.fetchone()
            if row and row.get("value"):
                until = row["value"]
                if isinstance(until, str):
                    until = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if until and datetime.now(UTC) < until:
                    return True
    except Exception:
        pass
    return False


def _guard(job, mode):
    auto = _load_automation()
    if _is_paused():
        return False, "automation paused"
    daily_cap = auto.get("daily_cap", 5)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM apply_events WHERE started_at > NOW() - INTERVAL '1 day' AND status = 'SUBMITTED';"
            )
            if cur.fetchone()["c"] >= daily_cap:
                return False, "daily cap reached"
            cooldown = auto.get("per_company_cooldown_hours", 72)
            cur.execute(
                "SELECT MAX(started_at) AS last FROM apply_events WHERE job_id IN (SELECT id FROM jobs WHERE company = %s) AND status = 'SUBMITTED';",
                (job["company"],),
            )
            row = cur.fetchone()
            if row and row.get("last"):
                last = row["last"]
                if datetime.now(UTC) - last < timedelta(hours=cooldown):
                    return False, "company cooldown"
    # If human explicitly marked interest/applied, allow review/prep regardless of raw automated thresholds
    is_human_interest = job.get("human_action") in ("APPLIED", "INTERESTED") or job.get("workflow_stage") in (
        "PROCESSING",
        "EDITING",
    )
    if not is_human_interest:
        if job.get("jev_fit_prob", 0) < auto.get("min_fit_prob", 0.70):
            return False, "fit prob below threshold"
        if job.get("jev_fit_tier") not in auto.get("allowed_tiers", []):
            return False, "tier not allowed"
    return True, "ok"


def _domain_allowed(url):
    auto = _load_automation()
    allowed = auto.get("allowed_apply_domains") or []
    host = urlparse(url).hostname or ""
    if not allowed:
        return True
    return any(host == d or host.endswith("." + d) for d in allowed)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


async def prepare_application(job):
    """Prepare an application (prefill + evidence). Never submits."""
    # Ensure complete job row is loaded from DB if partial dict provided
    job_id = job.get("id") if isinstance(job, dict) else job
    if job_id:
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

    # Ensure tailored PDF exists on disk (regenerating from JSON if needed)
    pdf_path = await ensure_tailored_pdf(job)
    if pdf_path:
        job["tailored_pdf_path"] = pdf_path

    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    pdf_sha = job.get("tailored_pdf_sha256")
    if pdf_path and Path(pdf_path).exists() and not pdf_sha:
        pdf_sha = await asyncio.to_thread(_sha256_file, pdf_path)
    if not pdf_path or not Path(pdf_path).exists():
        return {"status": "ABORTED", "error": "missing tailored PDF"}

    if not _domain_allowed(job["apply_url"]):
        def _freeze_domain():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET apply_feasible=FALSE, apply_blocker='domain not allowed', apply_state='NEEDS_REVIEW' WHERE id=%s;",
                    (job["id"],),
                )
                conn.commit()

        await asyncio.to_thread(_freeze_domain)
        return {"status": "ABORTED", "error": "domain not allowed"}

    ok, reason = await asyncio.to_thread(_guard, job, "CONFIRM")
    if not ok:
        return {"status": "ABORTED", "error": reason}

    evidence_dir = Path("output") / "apply_evidence" / str(job["id"])
    evidence_dir.mkdir(parents=True, exist_ok=True)

    screenshots = []
    fields_filled = {}
    status = "PREPARED"
    error = None
    router_path = "MANUAL_COMPLEX"
    blocker = None

    # Merge website classification + prefill into a single Chromium launch
    async with _BROWSER_SEM:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(job["apply_url"], timeout=30000)

                # Dismiss cookie/modal popups & trigger form open if needed
                await dismiss_popups_and_open_form(page)

                # Classify form DOM directly on open page
                router_path, _rreason = await _analyze_page_dom(page)
                feasible = router_path == "AUTO_SIMPLE"
                blocker = None if feasible else (_rreason or router_path)

                def _save_feasibility():
                    with get_connection() as conn, conn.cursor() as cur:
                        cur.execute(
                            "UPDATE jobs SET apply_feasible=%s, apply_blocker=%s WHERE id=%s;",
                            (feasible, blocker, job["id"]),
                        )
                        if not feasible:
                            cur.execute(
                                "UPDATE jobs SET apply_state='NEEDS_REVIEW' WHERE id=%s;",
                                (job["id"],),
                            )
                            cur.execute(
                                "INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, started_at, finished_at) VALUES (%s,%s,%s,%s,%s,%s,NOW(),NOW());",
                                (job["id"], 1, router_path, "PREPARE", "ESCALATED", blocker),
                            )
                        conn.commit()

                await asyncio.to_thread(_save_feasibility)

                if not feasible:
                    sp_esc = str(evidence_dir / "step_escalated.png")
                    await page.screenshot(path=sp_esc)
                    await browser.close()
                    alert(f"🔒 {job['company']} not auto-applicable ({blocker}) — apply manually: {job['apply_url']}")
                    return {
                        "status": "ESCALATED",
                        "router_path": router_path,
                        "error": blocker,
                        "feasible": False,
                        "screenshots": [sp_esc],
                    }

                # Feasible! Capture initial view & prefill
                sp0 = str(evidence_dir / "step_0.png")
                await page.screenshot(path=sp0)
                screenshots.append(sp0)

                profile = get_master_profile()
                fields_filled = await fill(page, job, profile)

                sp_filled = str(evidence_dir / "step_filled.png")
                await page.screenshot(path=sp_filled)
                screenshots.append(sp_filled)

                await browser.close()
        except Exception as e:
            status = "FAILED"
            error = str(e)
            alert(f"⚠️ Prepare failed for {job['company']}: {e}")

    def _persist_prepare_results():
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, fields_filled, screenshots, started_at, finished_at, evidence_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s);
                """,
                (
                    job["id"],
                    1,
                    router_path,
                    "PREPARE",
                    status,
                    error,
                    Jsonb(fields_filled),
                    screenshots,
                    str(evidence_dir),
                ),
            )
            cur.execute(
                "UPDATE jobs SET apply_status=%s, apply_state=%s, apply_last_at=NOW(), tailored_pdf_path=%s, tailored_pdf_sha256=%s WHERE id=%s;",
                (status, status, pdf_path, pdf_sha, job["id"]),
            )
            conn.commit()

    await asyncio.to_thread(_persist_prepare_results)
    return {
        "status": status,
        "router_path": router_path,
        "error": error,
        "fields_filled": fields_filled,
        "screenshots": screenshots,
        "feasible": True,
    }


async def submit_application(job, confirmation_token):
    """Submit only with a valid, job-bound confirmation token."""
    row = await asyncio.to_thread(consume_apply_confirmation, confirmation_token, job_id=job["id"], kind="SUBMIT")
    if not row:
        return {"status": "REJECTED", "error": "invalid/expired/used token"}
    payload = row.get("payload") or {}

    # Ensure tailored PDF exists
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

    # perform submit
    evidence_dir = Path("output") / "apply_evidence" / str(job["id"])
    evidence_dir.mkdir(parents=True, exist_ok=True)
    router_path, _rreason = await classify(job["apply_url"])
    screenshots = []
    fields_filled = {}
    status = "SUBMITTED"
    error = None
    verification = "unverified"
    final_url = None

    async with _BROWSER_SEM:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(job["apply_url"], timeout=30000)
                profile = get_master_profile()
                fields_filled = await fill(page, job, profile)
                # Robust multi-strategy submit button detection + click
                submit_selectors = [
                    "button[type=submit]",
                    "input[type=submit]",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "button:has-text('Apply')",
                    "a[role='button']:has-text('Submit')",
                    "button[class*='submit']",
                    "input[value*='Submit' i]",
                ]
                submit = None
                for sel in submit_selectors:
                    try:
                        submit = await page.query_selector(sel)
                        if submit and await submit.is_visible():
                            break
                        submit = None
                    except Exception:
                        submit = None

                # Try to resolve the submit button within any active frame
                if not submit:
                    for frame in page.frames:
                        if frame == page.main_frame:
                            continue
                        for sel in submit_selectors:
                            try:
                                submit = await frame.query_selector(sel)
                                if submit and await submit.is_visible():
                                    break
                            except Exception:
                                submit = None
                        if submit:
                            break

                if not submit:
                    # Try broader fallback selector (any visible button with submit/apply keywords)
                    submit = await page.query_selector("button:has-text('Submit'), button:has-text('Apply'), input[type=submit]")

                if not submit:
                    status = "FAILED"
                    error = "no submit button found after multi-strategy search"
                else:
                    # Pre-submit validation checks: detect visible validation errors / missing required fields (smart filtering)
                    pre_errors = []
                    try:
                        # Only check for actual visible validation UI patterns, not random keywords
                        # Look for elements with error/invalid classes that contain error text
                        error_containers = await page.query_selector_all(
                            '.error, .invalid, [class*="error"], [class*="invalid"], [aria-invalid="true"]'
                        )
                        visible_errors = []
                        for ec in error_containers:
                            if await ec.is_visible():
                                text = await ec.inner_text() if hasattr(ec, 'inner_text') else (await ec.text_content() if hasattr(ec, 'text_content') else "")
                                text_lower = (text or "").lower()
                                # Only count if it contains actual error indicators
                                if any(ind in text_lower for ind in ["required", "missing", "invalid", "error", "please", "empty", "fix"]):
                                    visible_errors.append(ec)
                        if len(visible_errors) > 0:
                            pre_errors.append(f"visible validation errors detected before submit ({len(visible_errors)} elements)")
                        # Check if any visible required inputs are empty (exclude file inputs which are handled separately)
                        required_empty = await page.evaluate("""() => {
                            const empties = [];
                            document.querySelectorAll('input, select, textarea').forEach(el => {
                                if (el.closest('[style*="display: none"]') || el.closest('[style*="visibility: hidden"]')) return;
                                const rect = el.getBoundingClientRect();
                                const isVisible = rect.width > 0 && rect.height > 0 && !!(el.offsetParent);
                                if (!isVisible) return;
                                const isRequired = el.required || el.getAttribute('aria-required') === 'true' || el.classList.contains('required') || el.closest('label, .form-group')?.querySelector('.required, [class*="required"]');
                                const isFile = el.type === 'file';
                                // Skip file inputs (resume upload handled separately)
                                if (isFile) return;
                                const isTextEmpty = (el.value || '').trim().length === 0;
                                const isSelectEmpty = el.tagName === 'SELECT' && (!el.value || el.value === '' || el.selectedIndex === -1);
                                if (isRequired && (isTextEmpty || isSelectEmpty)) {
                                    const label = el.closest('label, .form-group, .field, [class*="field"], tr, div');
                                    const labelText = label ? (label.innerText || label.getAttribute('aria-label') || '').trim() : '';
                                    const lbl = labelText || el.name || el.id || 'field';
                                    // Only add if it's a real field (not hidden or decorative)
                                    empties.push(lbl);
                                }
                            });
                            return empties;
                        }""")
                        # Filter out false positives (labels containing only decorative markers)
                        filtered_empties = [r for r in (required_empty or []) if len(str(r)) > 2 and not str(r).startswith('field_') and not str(r).startswith('unknown')]
                        # Only count clearly real empty fields (exclude decorative labels)
                        real_empty_fields = []
                        for r in filtered_empties:
                            lbl_text = str(r).lower()
                            # Skip decorative/placeholder-only labels
                            if lbl_text in ['select', '--select--', '--select--'] or 'choose' in lbl_text or lbl_text.startswith('field_'):
                                continue
                            real_empty_fields.append(r)
                        if real_empty_fields and len(real_empty_fields) > 0:
                            pre_errors.append(f"empty required fields before submit: {', '.join(str(r) for r in real_empty_fields[:3])}")
                    except Exception as e:
                        logger.debug("Pre-submit validation check failed: %s", e)

                    if pre_errors:
                        status = "FAILED"
                        error = f"pre-submit validation failed: {'; '.join(pre_errors)}"
                    else:
                        # Before clicking: scroll button into view and wait for stability
                        try:
                            await submit.scroll_into_view_if_needed(timeout=5000)
                        except Exception:
                            pass
                        try:
                            await submit.wait_for_element_state("stable", timeout=15000)
                        except Exception:
                            pass

                        # Capture pre-submit screenshot for debugging
                        pre_submit_sp = str(evidence_dir / "step_before_submit.png")
                        try:
                            await page.screenshot(path=pre_submit_sp)
                            screenshots.append(pre_submit_sp)
                        except Exception:
                            pass

                        # Try standard click with retry
                        click_ok = False
                        for attempt in range(3):
                            try:
                                await submit.click(timeout=15000)
                                click_ok = True
                                break
                            except Exception:
                                # Fallback to JS click if Playwright click fails (element covered, etc.)
                                try:
                                    await submit.evaluate("el => el.click()")
                                    click_ok = True
                                    break
                                except Exception:
                                    await page.wait_for_timeout(500 + (attempt * 300))
                                    continue

                        if not click_ok:
                            status = "FAILED"
                            error = "submit button click failed after retries (visible/stable issue)"
                        else:
                            await page.wait_for_timeout(3000)
                            sp = str(evidence_dir / "step_submitted.png")
                            await page.screenshot(path=sp)
                            screenshots.append(sp)
                            final_url = page.url
                            content = (await page.content()).lower()

                        login_markers = ["sign in", "log in", "password", "create an account"]
                        captcha_markers = ["recaptcha", "hcaptcha", "turnstile", "cf-turnstile"]
                        error_markers = ["required", "please fix", "invalid", "error", "missing"]
                        confirm_markers = ["thank you", "application received", "submitted", "confirmation", "success"]
                        has_login = any(m in content for m in login_markers)
                        has_captcha = any(m in content for m in captcha_markers)
                        has_error = any(m in content for m in error_markers)
                        has_confirm = any(m in content for m in confirm_markers)
                        url_changed = final_url and final_url != job["apply_url"]
                        url_confirm = any(
                            m in (final_url or "").lower() for m in ("thank", "confirm", "success", "submitted")
                        )

                        if has_captcha:
                            verification = "captcha"
                            status = "NEEDS_REVIEW"
                            error = "captcha detected after submit"
                        elif has_login:
                            verification = "login_required"
                            status = "NEEDS_REVIEW"
                            error = "login required after submit"
                        elif has_error and not has_confirm:
                            verification = "validation_error"
                            status = "FAILED"
                            error = "form validation error after submit"
                        elif has_confirm or url_confirm or (url_changed and not has_error):
                            verification = "verified"
                            status = "SUBMITTED"
                        else:
                            verification = "ambiguous"
                            status = "NEEDS_REVIEW"
                await browser.close()
        except Exception as e:
            status = "FAILED"
            error = str(e)
            alert(f"⚠️ Submit failed for {job['company']}: {e}")

    if status == "SUBMITTED" and verification != "verified":
        status = "NEEDS_REVIEW"

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
        alert(f"⚠️ Apply not confirmed for {job['company']}: {status} {error or ''}")
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
        job = dict(j)
        await prepare_application(job)


async def prepare_apply_cycle(limit=3):
    await auto_apply_cycle(limit=limit)
