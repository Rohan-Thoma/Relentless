import os
import yaml
from datetime import datetime, timedelta, UTC
from src.db import get_connection
from src.settings_store import get_master_profile
from src.apply.detector import classify
from src.apply.autofill import fill
from src.notify import alert

CONFIG_PATH = "config/config.yaml"


def _load_automation():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    auto = cfg.get("automation", {})
    # overlay from app_settings if present
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
    # daily cap
    daily_cap = auto.get("daily_cap", 5)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM apply_events WHERE started_at > NOW() - INTERVAL '1 day' AND status = 'SUBMITTED';"
            )
            if cur.fetchone()["c"] >= daily_cap:
                return False, "daily cap reached"
            # cooldown per company
            cooldown = auto.get("per_company_cooldown_hours", 72)
            cur.execute(
                "SELECT MAX(started_at) AS last FROM apply_events WHERE job_id IN (SELECT id FROM jobs WHERE company = %s);",
                (job["company"],),
            )
            row = cur.fetchone()
            if row and row.get("last"):
                last = row["last"]
                if datetime.now(UTC) - last < timedelta(hours=cooldown):
                    return False, "company cooldown"
    # fit prob / tier
    if job.get("jev_fit_prob", 0) < auto.get("min_fit_prob", 0.70):
        return False, "fit prob below threshold"
    if job.get("jev_fit_tier") not in auto.get("allowed_tiers", []):
        return False, "tier not allowed"
    return True, "ok"


async def attempt_apply(job, mode=None):
    auto = _load_automation()
    if mode is None:
        mode = auto.get("apply_mode", "DRY_RUN")
    ok, reason = _guard(job, mode)
    if not ok:
        return {"status": "ABORTED", "error": reason}
    if mode == "DRY_RUN":
        pass
    screenshots = []
    evidence_dir = os.path.join("output", "apply_evidence", str(job["id"]))
    os.makedirs(evidence_dir, exist_ok=True)
    router_path, rreason = await classify(job["apply_url"])
    profile = get_master_profile()
    job["pdf_path"] = None  # caller may set
    started = datetime.now(UTC)
    fields_filled = {}
    status = "ESCALATED"
    error = None
    # escalation paths
    if router_path in ("NEEDS_LOGIN", "CAPTCHA", "COMPLEX", "BLOCKED"):
        status = "ESCALATED"
        error = rreason
        msg = f"🔒 {job['company']} requires login/complex — apply manually: {job['apply_url']}"
        alert(msg)
    else:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(job["apply_url"], timeout=30000)
                # screenshot step 0
                sp = os.path.join(evidence_dir, "step_0.png")
                await page.screenshot(path=sp)
                screenshots.append(sp)
                fields_filled = await fill(page, job, profile)
                # dry-run: don't submit unless AUTO
                if mode in ("AUTO_EASY", "AUTO_ALL") and router_path == "AUTO_SIMPLE":
                    # attempt submit
                    submit = await page.query_selector("button[type=submit], input[type=submit]")
                    if submit:
                        await submit.click()
                        await page.wait_for_timeout(2000)
                        sp2 = os.path.join(evidence_dir, "step_final.png")
                        await page.screenshot(path=sp2)
                        screenshots.append(sp2)
                        status = "SUBMITTED"
                else:
                    # CONFIRM or DRY_RUN: screenshot filled state, escalate for confirmation
                    sp2 = os.path.join(evidence_dir, "step_filled.png")
                    await page.screenshot(path=sp2)
                    screenshots.append(sp2)
                    if mode == "CONFIRM":
                        status = "ESCALATED"
                    else:
                        status = "DRY_RUN" if mode == "DRY_RUN" else "ESCALATED"
                await browser.close()
        except Exception as e:
            status = "FAILED"
            error = str(e)
            alert(f"⚠️ Apply failed for {job['company']}: {e}")

    # write apply_events
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_events (job_id, attempt, router_path, mode, status, error, fields_filled, screenshots, started_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW());
                """,
                (job["id"], 1, router_path, mode, status, error, fields_filled, screenshots, started),
            )
            cur.execute(
                "UPDATE jobs SET apply_status = %s, apply_last_at = NOW() WHERE id = %s;",
                (status, job["id"]),
            )
        conn.commit()
    return {"status": status, "router_path": router_path, "error": error, "fields_filled": fields_filled, "screenshots": screenshots}


async def auto_apply_cycle(limit=3):
    auto = _load_automation()
    mode = auto.get("apply_mode", "DRY_RUN")
    if not mode.startswith("AUTO"):
        return
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT id, company, title, apply_url, jev_fit_prob, jev_fit_tier
                FROM jobs
                WHERE triage_passed = TRUE AND human_action IS NULL AND apply_status = 'NOT_APPLIED'
                ORDER BY jev_fit_prob DESC
                LIMIT %s;
                """,
            (limit,),
        )
        jobs = cur.fetchall()
    for j in jobs:
        job = dict(j)
        await attempt_apply(job, mode=mode)