import asyncio
import collections
from datetime import UTC, datetime, timedelta
import fcntl
import json
import logging
import os
import re as _re

from dotenv import load_dotenv
import httpx
from psycopg.types.json import Jsonb

from evals.evaluate_triage import run_evaluation
from src.apply.runner import prepare_application, submit_application
from src.db import (
    consume_pending_action,
    create_apply_confirmation,
    create_pending_action,
    delete_pending_action,
    get_connection,
    reset_database,
)
from src.hitl.auth import is_authorized, update_chat_id
from src.knowledge.distiller import run_distill
from src.knowledge.store import get_store
from src.notify import alert
from src.tailor.resume_tailor import tailor_resume_for_job

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

logger = logging.getLogger(__name__)

# In-memory map of active edit job per chat
_ACTIVE_EDIT_JOB: dict[int, int] = {}

# In-memory deduplication of update IDs
_PROCESSED_UPDATE_IDS = collections.deque(maxlen=1000)

_BOT_LOCK_FILE = None


def acquire_bot_lock() -> bool:
    """Acquires exclusive, non-blocking process lock for the Telegram listener."""
    global _BOT_LOCK_FILE
    lock_path = "/tmp/relentless_telegram_bot.lock"
    try:
        f = open(lock_path, "a+")  # noqa: SIM115
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()}\n")
        f.flush()
        _BOT_LOCK_FILE = f
        return True
    except (BlockingIOError, OSError):
        return False


def get_active_bot_pid() -> str:
    """Reads PID of the process currently holding the bot lock."""
    lock_path = "/tmp/relentless_telegram_bot.lock"
    try:
        with open(lock_path, "r") as f:
            return f.read().strip()
    except Exception:
        return "unknown"


def release_bot_lock():
    """Releases the bot listener process lock."""
    global _BOT_LOCK_FILE
    if _BOT_LOCK_FILE:
        try:
            fcntl.flock(_BOT_LOCK_FILE.fileno(), fcntl.LOCK_UN)
            _BOT_LOCK_FILE.close()
        except Exception:
            pass
        _BOT_LOCK_FILE = None


def build_inline_keyboard(job_id: int) -> dict:
    """Builds discovery buttons for new job cards."""
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Interested (Tailor & Check Site)", "callback_data": f"interest:{job_id}"},
                {"text": "👎 Skip: Bad Fit", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"},
            ],
            [
                {"text": "🚫 Skip: Ghost/Scam", "callback_data": f"act:SKIP_SCAM:{job_id}"},
                {"text": "⭐ Got Callback!", "callback_data": f"out:CALLBACK:{job_id}"},
            ],
        ]
    }


def _record_bot_message(message_id: int, chat_id: int, job_id: int):
    """Maps Telegram message ID to job_id so replies can be resolved automatically."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telegram_message_jobs (message_id, chat_id, job_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (message_id) DO UPDATE SET job_id = EXCLUDED.job_id;
                """,
                (message_id, chat_id, job_id),
            )
            cur.execute(
                "UPDATE jobs SET last_telegram_message_id = %s WHERE id = %s;",
                (message_id, job_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Could not record telegram message %s: %s", message_id, e)


def _find_job_for_message(reply_msg: dict) -> int | None:
    """Finds the job ID for a replied-to message using DB mapping or regex fallback."""
    if not reply_msg or not isinstance(reply_msg, dict):
        return None
    mid = reply_msg.get("message_id")
    if mid:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT job_id FROM telegram_message_jobs WHERE message_id = %s;", (mid,))
                row = cur.fetchone()
                if row and row.get("job_id"):
                    return row["job_id"]
        except Exception:
            pass
    # Regex fallback on text or caption
    full_text = f"{reply_msg.get('text', '')} {reply_msg.get('caption', '')}"
    m = _re.search(r"Job\s*#(\d+)", full_text, _re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _append_user_edit_note(job_id: int, note: str):
    """Appends an edit instruction note to jobs.user_edit_notes."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET user_edit_notes = COALESCE(user_edit_notes, '[]'::jsonb) || %s::jsonb,
                    workflow_stage = 'EDITING'
                WHERE id = %s;
                """,
                (json.dumps([note]), job_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Could not append user edit note: %s", e)


async def send_pending_digests(limit: int = 5):
    """Sends newly triaged, unnotified jobs to Telegram without duplicate spam."""
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_ats, company, title, location, apply_url,
                   jev_fit_prob, jev_fit_prob_cal, jev_fit_tier, jev_scam_prob, jev_persona, cosine_sim
            FROM jobs
            WHERE triage_passed = TRUE
              AND human_action IS NULL
              AND notified_at IS NULL
            ORDER BY COALESCE(jev_fit_prob_cal, jev_fit_prob) DESC
            LIMIT %s;
            """,
            (limit,),
        )
        jobs = cur.fetchall()

    if not jobs:
        print("📭 No unnotified triaged jobs to send right now.")
        return

    async with httpx.AsyncClient(timeout=15.0) as client:
        for j in jobs:
            effective_fit = j.get("jev_fit_prob_cal") or j.get("jev_fit_prob") or 0.0
            fit_pct = int(effective_fit * 100)
            scam_pct = int((j["jev_scam_prob"] or 0) * 100)
            text = (
                f"🎯 *{j['company']}* — *{j['title']}* (Job #{j['id']})\n"
                f"📍 `{j['location']}` | 🏢 ATS: `{j['source_ats']}`\n\n"
                f"🧠 *System-1 Jev Triage:*\n"
                f"• Fit Probability: *{fit_pct}%* (`{j['jev_fit_tier']}`)\n"
                f"• Scam/Ghost Risk: *{scam_pct}%*\n"
                f"• Baseline Sim: `{(j['cosine_sim'] or 0):.2f}`\n"
                f"• Persona Route: `{j['jev_persona']}`\n\n"
                f"🔗 [Open Direct Application Page]({j['apply_url']})"
            )
            for attempt in range(3):
                try:
                    resp = await client.post(
                        f"{TG_BASE}/sendMessage",
                        json={
                            "chat_id": CHAT_ID,
                            "text": text,
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                            "reply_markup": build_inline_keyboard(j["id"]),
                        },
                    )
                    if resp.status_code == 200:
                        mid = resp.json().get("result", {}).get("message_id")
                        if mid:
                            _record_bot_message(mid, int(CHAT_ID), j["id"])
                        with get_connection() as c, c.cursor() as cur:
                            cur.execute(
                                "UPDATE jobs SET notified_at = NOW(), workflow_stage = 'NOTIFIED' WHERE id = %s;",
                                (j["id"],),
                            )
                            c.commit()
                        print(f"📲 Sent Telegram card for [{j['company']}] {j['title']} (Job #{j['id']})")
                        break
                    elif resp.status_code == 429:
                        retry_after = int(resp.json().get("parameters", {}).get("retry_after", 2))
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        print(f"⚠️ Telegram send failed: {resp.status_code} {resp.text[:100]}")
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(1 * (attempt + 1))
                        continue
                    print(f"❌ Failed to send Telegram card after 3 attempts: {e}")
            await asyncio.sleep(0.4)


async def _process_job_interest(
    job_row: dict, chat_id: int, custom_note: str = "", is_edit: bool = False
):
    """Parallel Tailor PDF + Site Feasibility Check -> Sends Review Card with Action Buttons."""
    job_id = job_row["id"]
    company = job_row["company"]
    title = job_row["title"]

    try:
        # Run parallel tailoring & site inspection
        async def _tailor_step():
            return await tailor_resume_for_job(job_row, custom_instruction=custom_note)

        async def _site_step():
            return await prepare_application(job_row)

        tailor_res, site_res = await asyncio.gather(_tailor_step(), _site_step(), return_exceptions=True)

        if isinstance(tailor_res, Exception):
            raise tailor_res

        pdf_path = tailor_res["pdf_path"]
        pdf_sha = tailor_res["pdf_sha256"]
        diff_summary = tailor_res["diff_summary"]

        feasible = False
        blocker = "Site check in progress"
        if not isinstance(site_res, Exception) and isinstance(site_res, dict):
            feasible = site_res.get("feasible", False)
            blocker = site_res.get("error") or ("Direct form ready" if feasible else "Blocked")

        site_status_line = (
            "🟢 *Website Check: Direct application form detected & pre-filled! Ready for 1-tap auto-submit.*"
            if feasible
            else f"🟡 *Website Check: Auto-submit blocked ({blocker}). Use the direct link below to submit your tailored PDF manually!*"
        )

        edit_header = (
            f"👋 *Hey! For Job #{job_id} ({company} — {title}), here is your updated PDF:*\n"
            f"*Requested:* \"{custom_note}\"\n\n"
            if is_edit
            else f"🎯 *Tailored Application Package — Job #{job_id}*\n*{company}* — *{title}*\n\n"
        )

        caption = (
            f"{edit_header}"
            f"{site_status_line}\n\n"
            f"🔗 [Direct Application Link]({job_row['apply_url']})\n\n"
            f"{diff_summary}"
        )

        buttons = []
        if feasible:
            raw_token, _ = create_apply_confirmation(
                job_id,
                kind="SUBMIT",
                payload={"pdf_sha256": pdf_sha, "apply_url": job_row["apply_url"]},
                ttl_seconds=1800,
                created_by=str(chat_id),
            )
            buttons.append([{"text": "🚀 Auto-Submit Now", "callback_data": f"submit:{job_id}:{raw_token}"}])

        buttons.append([
            {"text": "✏️ Request Resume Changes", "callback_data": f"edit_prompt:{job_id}"},
            {"text": "✅ Mark Applied Manually", "callback_data": f"manual_applied:{job_id}"},
        ])
        buttons.append([
            {"text": "🔄 Re-check Website", "callback_data": f"prepare:{job_id}"},
            {"text": "❌ Drop Job", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"},
        ])

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(pdf_path, "rb") as pdf_file:
                resp = await client.post(
                    f"{TG_BASE}/sendDocument",
                    data={
                        "chat_id": chat_id,
                        "caption": caption[:1024],
                        "parse_mode": "Markdown",
                        "reply_markup": json.dumps({"inline_keyboard": buttons}),
                    },
                    files={"document": (os.path.basename(pdf_path), pdf_file, "application/pdf")},
                )
                if resp.status_code == 200:
                    mid = resp.json().get("result", {}).get("message_id")
                    if mid:
                        _record_bot_message(mid, chat_id, job_id)
                else:
                    # Clean fallback if Markdown parsing fails
                    clean_caption = caption[:1024].replace("*", "").replace("`", "").replace("_", "")
                    resp = await client.post(
                        f"{TG_BASE}/sendDocument",
                        data={
                            "chat_id": chat_id,
                            "caption": clean_caption,
                            "reply_markup": json.dumps({"inline_keyboard": buttons}),
                        },
                        files={"document": (os.path.basename(pdf_path), pdf_file, "application/pdf")},
                    )
                    mid = resp.json().get("result", {}).get("message_id")
                    if mid:
                        _record_bot_message(mid, chat_id, job_id)

    except Exception as e:
        logger.error("Error processing interest for job #%s: %s", job_id, e)
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{TG_BASE}/sendMessage",
                json={"chat_id": chat_id, "text": f"⚠️ Job #{job_id} ({company}): processing failed: {e}"},
            )


async def handle_slash_command(client: httpx.AsyncClient, cmd_text: str, chat_id=None):
    if chat_id is None:
        chat_id = CHAT_ID
    cmd = cmd_text.strip().split()[0].lower()

    if cmd in ("/start", "/help"):
        reply = (
            "🤖 *Relentless v2.0 Control Center*\n\n"
            "• `/digest` — Send top unreviewed job matches\n"
            "• `/status` — Check database & label counts\n"
            "• `/brain` — View live OKF Brain & Market Intelligence\n"
            "• `/eval` — View live Precision@K & calibration report\n"
            "• `/reset_labels` — Soft Reset: clear button clicks (`n=0`)\n"
            "• `/reset_triage` — Medium Reset: clear Jev scores + labels\n"
            "• `/wipe_all` — Hard Reset: delete all scraped jobs & start fresh"
        )
    elif cmd == "/digest":
        await send_pending_digests(limit=5)
        return
    elif cmd == "/status":
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM jobs;")
            total = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE triage_passed = TRUE;")
            passed = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE human_action IS NOT NULL;")
            labeled = cur.fetchone()["c"]
        reply = (
            f"📊 *Database Status*\n"
            f"• Total Scraped Jobs: `{total}`\n"
            f"• Passed System-1 Triage: `{passed}`\n"
            f"• Human Labeled (`n`): `{labeled}`"
        )
    elif cmd == "/brain":
        store = get_store()
        store.hydrate()
        hot = store.get("market/hot-skills")
        vel = store.get("market/company-hiring-velocity")
        trends = store.get("market/india-remote-trends")
        edits = store.get("preferences/resume-edit-habits")
        scams = store.search("scam", type="Scam Pattern")

        summary_parts = ["🧠 *Relentless OKF Brain & Market Intelligence*"]
        if hot:
            summary_parts.append(f"\n🔥 *Hot Skills:* {hot.body[:350]}...")
        if vel:
            summary_parts.append(f"\n🚀 *Hiring Velocity:* {vel.body[:300]}...")
        if trends:
            summary_parts.append(f"\n📍 *Geographic Trends:* {trends.body[:250]}...")
        if edits:
            summary_parts.append(f"\n📝 *Learned Resume Edit Habits:* {edits.body[:300]}...")
        summary_parts.append(f"\n🛡️ *Known Scam Patterns:* `{len(scams)}` flagged")

        reply = "\n".join(summary_parts)
    elif cmd == "/eval":
        reply = run_evaluation()
    elif cmd in ("/reset_labels", "/reset_triage", "/wipe_all"):
        mode_map = {"/reset_labels": "labels", "/reset_triage": "triage", "/wipe_all": "all"}
        mode = mode_map[cmd]
        raw_token, _expires = create_pending_action(
            "DESTRUCTIVE", {"mode": mode, "chat_id": int(chat_id) if chat_id else None}, ttl_seconds=600
        )
        confirm_text = (
            f"⚠️ Confirm destructive action: `{cmd}` (mode={mode})?\n"
            "This cannot be undone. Expires in 10 minutes."
        )
        await client.post(
            f"{TG_BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": confirm_text,
                "parse_mode": "Markdown",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Confirm", "callback_data": f"confirm:{raw_token}"},
                            {"text": "❌ Cancel", "callback_data": f"cancel:{raw_token}"},
                        ]
                    ]
                },
            },
        )
        return
    elif cmd == "/stop_apply":
        until = datetime.now(UTC) + timedelta(hours=24)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('automation_paused_until', %s, NOW()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
                    (Jsonb(until.isoformat()),),
                )
            conn.commit()
        reply = f"⏸️ Auto-apply paused until {until.isoformat()}"
    elif cmd == "/apply_status":
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, status, router_path, started_at FROM apply_events ORDER BY started_at DESC LIMIT 10;"
                )
                rows = cur.fetchall()
        lines = ["📋 Last apply events:"]
        for r in rows:
            lines.append(f"• Job {r['job_id']}: {r['status']} ({r['router_path']})")
        reply = "\n".join(lines) if rows else "No apply events yet."
    else:
        return

    await client.post(
        f"{TG_BASE}/sendMessage",
        json={"chat_id": chat_id or CHAT_ID, "text": reply, "parse_mode": "Markdown"},
    )


async def _listen_loop():
    offset = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                for attempt in range(3):
                    try:
                        resp = await client.get(f"{TG_BASE}/getUpdates", params={"offset": offset, "timeout": 25})
                        data = resp.json()
                        break
                    except Exception:
                        if attempt < 2:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        raise

                for upd in data.get("result", []):
                    upd_id = upd.get("update_id")
                    if upd_id is not None:
                        if upd_id in _PROCESSED_UPDATE_IDS:
                            offset = max(offset, upd_id + 1)
                            continue
                        _PROCESSED_UPDATE_IDS.append(upd_id)
                        offset = max(offset, upd_id + 1)

                    if not is_authorized(upd):
                        cb = upd.get("callback_query")
                        if cb:
                            try:
                                await client.post(
                                    f"{TG_BASE}/answerCallbackQuery",
                                    json={"callback_query_id": cb["id"], "text": "Not authorized"},
                                )
                            except Exception:
                                pass
                        continue

                    msg_obj = upd.get("message", {})
                    chat_id = update_chat_id(upd) or int(CHAT_ID)

                    # 1. Check for text messages / replies
                    text_in = msg_obj.get("text", "") if isinstance(msg_obj, dict) else ""
                    if isinstance(text_in, str) and text_in.strip():
                        # Slash commands
                        if text_in.startswith("/"):
                            await handle_slash_command(client, text_in, chat_id=chat_id)
                            continue

                        # Resolve job_id via 3 conversational methods
                        resolved_jid = None
                        edit_instruction = ""

                        # Method 1: User sent #<job_id> <instruction>
                        if text_in.strip().startswith("#"):
                            m = _re.match(r"^#(\d+)\s*(.*)$", text_in.strip(), _re.DOTALL)
                            if m:
                                resolved_jid = int(m.group(1))
                                edit_instruction = m.group(2).strip()

                        # Method 2: User replied to a bot card / PDF
                        elif msg_obj.get("reply_to_message"):
                            resolved_jid = _find_job_for_message(msg_obj["reply_to_message"])
                            edit_instruction = text_in.strip()

                        # Method 3: User previously tapped '✏️ Request Resume Changes'
                        elif chat_id in _ACTIVE_EDIT_JOB:
                            resolved_jid = _ACTIVE_EDIT_JOB.pop(chat_id)
                            edit_instruction = text_in.strip()

                        if resolved_jid:
                            with get_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "SELECT id, company, title, jd_text, jev_persona, apply_url, tailored_pdf_path, tailored_pdf_sha256 FROM jobs WHERE id = %s;",
                                        (resolved_jid,),
                                    )
                                    jr = cur.fetchone()

                            if jr:
                                _append_user_edit_note(resolved_jid, edit_instruction)
                                # Asynchronously trigger OKF distillation
                                asyncio.create_task(asyncio.to_thread(run_distill))

                                await client.post(
                                    f"{TG_BASE}/sendMessage",
                                    json={
                                        "chat_id": chat_id,
                                        "text": (
                                            f"📝 Noted for Job #{resolved_jid} ({jr['company']} — {jr['title']})!\n"
                                            f"Re-tailoring your resume with: \"{edit_instruction}\".\n"
                                            "I'll ping you here as soon as the updated PDF is ready!"
                                        ),
                                    },
                                )
                                # Spawn concurrent re-tailoring
                                asyncio.create_task(
                                    _process_job_interest(
                                        dict(jr), chat_id, custom_note=edit_instruction, is_edit=True
                                    )
                                )
                                continue

                    # 2. Check for inline button callbacks
                    cb = upd.get("callback_query")
                    if not cb:
                        continue

                    cb_id = cb["id"]
                    payload = cb.get("data", "")

                    # Confirm/cancel destructive actions
                    if payload.startswith(("confirm:", "cancel:")):
                        token = payload.split(":", 1)[1]
                        if payload.startswith("cancel:"):
                            delete_pending_action(token)
                            await client.post(
                                f"{TG_BASE}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Cancelled"},
                            )
                            continue
                        row = consume_pending_action(token, kind="DESTRUCTIVE")
                        if not row:
                            await client.post(
                                f"{TG_BASE}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Invalid/expired token"},
                            )
                            continue
                        payload_data = row["payload"] or {}
                        mode = payload_data.get("mode", "labels")
                        mode_to_reset = {"labels": "labels", "triage": "triage", "all": "all"}[mode]
                        try:
                            msg = reset_database(mode_to_reset)
                        except Exception as e:
                            logger.error("Failed to reset database: %s", e)
                            msg = f"❌ Reset failed: {e}"
                        await client.post(
                            f"{TG_BASE}/sendMessage",
                            json={"chat_id": chat_id or CHAT_ID, "text": msg},
                        )
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Done"},
                        )
                        continue

                    # Tap "Interested" (or legacy act:APPLIED)
                    if payload.startswith(("interest:", "act:APPLIED:")):
                        job_id_str = payload.split(":")[-1]
                        try:
                            job_id = int(job_id_str)
                        except ValueError:
                            continue

                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": f"Working on Job #{job_id}..."},
                        )

                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET human_action = 'APPLIED',
                                        human_action_at = NOW(),
                                        workflow_stage = 'PROCESSING'
                                    WHERE id = %s
                                    RETURNING id, company, title, jd_text, jev_persona, apply_url, tailored_pdf_path, tailored_pdf_sha256;
                                    """,
                                    (job_id,),
                                )
                                job_row = cur.fetchone()
                            conn.commit()

                        if job_row:
                            jr_dict = dict(job_row)
                            await client.post(
                                f"{TG_BASE}/sendMessage",
                                json={
                                    "chat_id": chat_id,
                                    "text": (
                                        f"👍 Got it! Working on Job #{job_id} ({jr_dict['company']} — {jr_dict['title']}) in the background: "
                                        "tailoring your resume & analyzing the application website. "
                                        "Feel free to review or give instructions on other jobs while I work!"
                                    ),
                                },
                            )
                            # Spawn parallel background task
                            asyncio.create_task(_process_job_interest(jr_dict, chat_id))
                            # Trigger background OKF brain distillation
                            asyncio.create_task(asyncio.to_thread(run_distill))
                        continue

                    # Tap "Edit Prompt"
                    if payload.startswith("edit_prompt:"):
                        try:
                            job_id = int(payload.split(":", 1)[1])
                        except ValueError:
                            continue
                        _ACTIVE_EDIT_JOB[chat_id] = job_id
                        with get_connection() as conn, conn.cursor() as cur:
                            cur.execute("SELECT company, title FROM jobs WHERE id = %s;", (job_id,))
                            jr = cur.fetchone()
                        co_name = jr["company"] if jr else f"Job #{job_id}"
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Type your resume changes:"},
                        )
                        await client.post(
                            f"{TG_BASE}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": (
                                    f"💬 Send me the changes you want for Job #{job_id} ({co_name})!\n"
                                    "Example: `Highlight my LangGraph RAG chatbot and add LangSmith observability`\n"
                                    "(You can also reply directly to any of my messages anytime)."
                                ),
                                "parse_mode": "Markdown",
                            },
                        )
                        continue

                    # Tap "Mark Applied Manually"
                    if payload.startswith("manual_applied:"):
                        try:
                            job_id = int(payload.split(":", 1)[1])
                        except ValueError:
                            continue
                        with get_connection() as conn, conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE jobs
                                SET human_action = 'APPLIED',
                                    human_action_at = NOW(),
                                    apply_status = 'MANUALLY_APPLIED',
                                    workflow_stage = 'APPLIED'
                                WHERE id = %s
                                RETURNING company, title;
                                """,
                                (job_id,),
                            )
                            jr = cur.fetchone()
                            conn.commit()
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Marked Applied!"},
                        )
                        co_name = jr["company"] if jr else f"Job #{job_id}"
                        await client.post(
                            f"{TG_BASE}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": f"🎉 Fantastic! Job #{job_id} ({co_name}) is marked as applied manually. Best of luck!",
                            },
                        )
                        asyncio.create_task(asyncio.to_thread(run_distill))
                        continue

                    # Tap "Submit" (submit:job_id:token)
                    if payload.startswith("submit:"):
                        parts = payload.split(":", 2)
                        if len(parts) != 3:
                            await client.post(
                                f"{TG_BASE}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Bad payload"},
                            )
                            continue
                        job_id = int(parts[1])
                        token = parts[2]
                        with get_connection() as conn, conn.cursor() as cur:
                            cur.execute(
                                "SELECT id, company, title, apply_url, tailored_pdf_path, tailored_pdf_sha256 FROM jobs WHERE id = %s;",
                                (job_id,),
                            )
                            job_row = cur.fetchone()
                        if job_row:
                            job_dict = dict(job_row)
                            res = await submit_application(job_dict, token)
                            st = res.get("status")
                            err = res.get("error") or ""
                            msg = (
                                f"🚀 Auto-Submit complete for {job_row['company']}: *{st}* {err}"
                                if st == "SUBMITTED"
                                else f"⚠️ Auto-Submit {st} for {job_row['company']}: {err}"
                            )
                            await client.post(
                                f"{TG_BASE}/sendMessage",
                                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                            )
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Submitted"},
                        )
                        continue

                    # Tap "Re-check Website" / Prepare
                    if payload.startswith("prepare:"):
                        try:
                            job_id = int(payload.split(":", 1)[1])
                        except ValueError:
                            continue
                        with get_connection() as conn, conn.cursor() as cur:
                            cur.execute(
                                "SELECT id, company, title, apply_url, jev_fit_prob, jev_fit_tier, jev_persona, tailored_pdf_path, tailored_pdf_sha256 FROM jobs WHERE id = %s;",
                                (job_id,),
                            )
                            job_row = cur.fetchone()
                        if job_row:
                            asyncio.create_task(_process_job_interest(dict(job_row), chat_id))
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Re-analyzing website..."},
                        )
                        continue

                    # Handle other act: / out: labels (e.g. SKIP_BAD_FIT, SKIP_SCAM, CALLBACK)
                    parts = payload.split(":")
                    if len(parts) == 3:
                        kind, label, job_id_str = parts[0], parts[1], parts[2]
                        try:
                            job_id = int(job_id_str)
                        except ValueError:
                            continue

                        with get_connection() as conn, conn.cursor() as cur:
                            if kind == "act":
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET human_action = %s, human_action_at = NOW(), workflow_stage = 'REVIEWED'
                                    WHERE id = %s
                                    RETURNING id, company, title;
                                    """,
                                    (label, job_id),
                                )
                            else:
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET outcome_status = %s, workflow_stage = 'CALLBACK'
                                    WHERE id = %s
                                    RETURNING id, company, title;
                                    """,
                                    (label, job_id),
                                )
                            jr = cur.fetchone()
                            conn.commit()

                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": f"Logged: {label}!"},
                        )
                        print(f"✅ Ground-Truth Logged -> Job #{job_id}: {label}")
                        asyncio.create_task(asyncio.to_thread(run_distill))
                        continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Telegram listener error: %s", e)
                alert(f"⚠️ Telegram listener error: {e}")
                await asyncio.sleep(3)


async def listen_for_button_clicks():
    if not CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID is required to run the Telegram listener")

    if not acquire_bot_lock():
        existing_pid = get_active_bot_pid()
        logger.warning(
            "⚠️ Telegram bot listener is already running in another process (PID %s)! "
            "Duplicate listener will wait to prevent duplicate command execution.",
            existing_pid,
        )
        print(
            f"⚠️ Telegram bot listener is already running in another process (PID {existing_pid})! "
            f"Waiting for existing instance to exit before taking over..."
        )
        while not acquire_bot_lock():
            await asyncio.sleep(5)
        print("🎧 Acquired Telegram bot lock. Starting listener...")

    print("🎧 Listening for Telegram button taps, replies & /commands (/help, /brain, /status)...")
    try:
        await _listen_loop()
    finally:
        release_bot_lock()


async def main():
    await send_pending_digests(limit=3)
    await listen_for_button_clicks()


if __name__ == "__main__":
    asyncio.run(main())
