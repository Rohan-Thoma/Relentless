import os
import hashlib
import yaml
from datetime import datetime, timedelta, UTC
from urllib.parse import urlparse

from src.db import get_connection, consume_apply_confirmation
from src.settings_store import get_master_profile
from src.apply.detector import classify
from src.apply.autofill import fill
from src.notify import alert

CONFIG_PATH = "config/config.yaml"


def _load_automation():
    auto = {}
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        auto = cfg.get("automation", {}) or {}
    except Exception:
        auto = {}
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'config_yaml';")
            row = cur.fetchone()
            if row and row.get("value"):
                auto = (row["value"].get("automation") or auto)
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
    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    pdf_sha = job.get("tailored_pdf_sha256")
    if pdf_path and os.path.exists(pdf_path) and not pdf_sha:
        pdf_sha = _sha256_file(pdf_path)
    if not pdf_path or not os.path.exists(pdf_path):
        return {"status": "ABORTED", "error": "missing tailored PDF"}
    if not _domain_allowed(job["apply_url"]):
        # freeze button
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET apply_feasible=FALSE, apply_blocker='domain not allowed', apply_state='NEEDS_REVIEW' WHERE id=%s;",
                    (job["id"],),
                )
            conn.commit()
        return {"status": "ABORTED", "error": "domain not allowed"}
    ok, reason = _guard(job, "CONFIRM")
    if not ok:
        return {"status": "ABORTED", "error": reason}

    evidence_dir = os.path.join("output", "apply_evidence", str(job["id"]))
    os.makedirs(evidence_dir, exist_ok=True)
    router_path, _rreason = await classify(job["apply_url"])

    # feasibility tagging
    feasible = router_path == "AUTO_SIMPLE"
    blocker = None if feasible else _rreason or router_path
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET apply_feasible=%s, apply_blocker=%s WHERE id=%s;",
                (feasible, blocker, job["id"]),
            )
        conn.commit()

    if not feasible:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET apply_state='NEEDS_REVIEW' WHERE id=%s;", (job["id"],)
                )
                cur.execute(
                    "INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, started_at, finished_at) VALUES (%s,%s,%s,%s,%s,%s,NOW(),NOW());",
                    (job["id"], 1, router_path, "PREPARE", "ESCALATED", blocker),
                )
            conn.commit()
        alert(f"🔒 {job['company']} not auto-applicable ({blocker}) — apply manually: {job['apply_url']}")
        return {"status": "ESCALATED", "router_path": router_path, "error": blocker, "feasible": False}

    # prefill (no submit)
    screenshots = []
    fields_filled = {}
    status = "PREPARED"
    error = None
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job["apply_url"], timeout=30000)
            sp = os.path.join(evidence_dir, "step_0.png")
            await page.screenshot(path=sp)
            screenshots.append(sp)
            profile = get_master_profile()
            fields_filled = await fill(page, job, profile)
            sp2 = os.path.join(evidence_dir, "step_filled.png")
            await page.screenshot(path=sp2)
            screenshots.append(sp2)
            await browser.close()
    except Exception as e:
        status = "FAILED"
        error = str(e)
        alert(f"⚠️ Prepare failed for {job['company']}: {e}")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, fields_filled, screenshots, started_at, finished_at, evidence_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s);
                """,
                (job["id"], 1, router_path, "PREPARE", status, error, fields_filled, screenshots, evidence_dir),
            )
            cur.execute(
                "UPDATE jobs SET apply_status=%s, apply_state=%s, apply_last_at=NOW(), tailored_pdf_path=%s, tailored_pdf_sha256=%s WHERE id=%s;",
                (status, status, pdf_path, pdf_sha, job["id"]),
            )
        conn.commit()
    return {"status": status, "router_path": router_path, "error": error, "fields_filled": fields_filled, "screenshots": screenshots, "feasible": True}


async def submit_application(job, confirmation_token):
    """Submit only with a valid, job-bound confirmation token."""
    row = consume_apply_confirmation(confirmation_token, job_id=job["id"], kind="SUBMIT")
    if not row:
        return {"status": "REJECTED", "error": "invalid/expired/used token"}
    payload = row.get("payload") or {}
    # verify pdf hash and url binding
    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    pdf_sha = job.get("tailored_pdf_sha256")
    if pdf_path and os.path.exists(pdf_path) and not pdf_sha:
        pdf_sha = _sha256_file(pdf_path)
    if payload.get("pdf_sha256") and payload.get("pdf_sha256") != pdf_sha:
        return {"status": "REJECTED", "error": "PDF hash mismatch"}
    if payload.get("apply_url") and payload.get("apply_url") != job["apply_url"]:
        return {"status": "REJECTED", "error": "apply URL mismatch"}
    if not _domain_allowed(job["apply_url"]):
        return {"status": "REJECTED", "error": "domain not allowed"}

    # perform submit
    evidence_dir = os.path.join("output", "apply_evidence", str(job["id"]))
    os.makedirs(evidence_dir, exist_ok=True)
    router_path, _rreason = await classify(job["apply_url"])
    screenshots = []
    fields_filled = {}
    status = "SUBMITTED"
    error = None
    verification = "unverified"
    final_url = None
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job["apply_url"], timeout=30000)
            profile = get_master_profile()
            fields_filled = await fill(page, job, profile)
            submit = await page.query_selector("button[type=submit], input[type=submit]")
            if not submit:
                status = "FAILED"
                error = "no submit button"
            else:
                await submit.click()
                await page.wait_for_timeout(3000)
                sp = os.path.join(evidence_dir, "step_submitted.png")
                await page.screenshot(path=sp)
                screenshots.append(sp)
                final_url = page.url
                content = (await page.content()).lower()
                # Rich multi-signal verification
                login_markers = ["sign in", "log in", "password", "create an account"]
                captcha_markers = ["recaptcha", "hcaptcha", "turnstile", "cf-turnstile"]
                error_markers = ["required", "please fix", "invalid", "error", "missing"]
                confirm_markers = ["thank you", "application received", "submitted", "confirmation", "success"]
                has_login = any(m in content for m in login_markers)
                has_captcha = any(m in content for m in captcha_markers)
                has_error = any(m in content for m in error_markers)
                has_confirm = any(m in content for m in confirm_markers)
                url_changed = final_url and final_url != job["apply_url"]
                url_confirm = any(m in (final_url or "").lower() for m in ("thank", "confirm", "success", "submitted"))
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, fields_filled, screenshots, confirmation_id, pdf_sha256, confirmed_by, verification_status, evidence_path, started_at, finished_at, submitted_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s);
                """,
                (
                    job["id"], 1, router_path, "SUBMIT", status, error, fields_filled, screenshots,
                    row["id"], pdf_sha, None, verification, evidence_dir,
                    datetime.now(UTC) if status == "SUBMITTED" else None,
                ),
            )
            cur.execute(
                "UPDATE jobs SET apply_status=%s, apply_state=%s, apply_last_at=NOW() WHERE id=%s;",
                (status, status, job["id"]),
            )
        conn.commit()
    if status != "SUBMITTED":
        alert(f"⚠️ Apply not confirmed for {job['company']}: {status} {error or ''}")
    return {"status": status, "router_path": router_path, "error": error, "verification": verification, "screenshots": screenshots}


async def attempt_apply(job, mode=None):
    """Backward-compatible wrapper: never submits."""
    if mode in ("AUTO_EASY", "AUTO_ALL", "AUTO"):
        return await prepare_application(job)
    return await prepare_application(job)


async def auto_apply_cycle(limit=3):
    # now prepare-only
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT id, company, title, apply_url, jev_fit_prob, jev_fit_tier, tailored_pdf_path, tailored_pdf_sha256
                FROM jobs
                WHERE triage_passed = TRUE AND human_action IS NULL
                  AND (apply_status IS NULL OR apply_status IN ('NOT_APPLIED','PREPARED'))
                ORDER BY jev_fit_prob DESC
                LIMIT %s;
            """,
            (limit,),
        )
        jobs = cur.fetchall()
    for j in jobs:
        job = dict(j)
        await prepare_application(job)


async def prepare_apply_cycle(limit=3):
    await auto_apply_cycle(limit=limit)