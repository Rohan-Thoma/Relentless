import asyncio
import collections
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
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
    try_acquire_advisory_lock,
)
from src.hitl.auth import is_authorized, update_chat_id
from src.knowledge.distiller import run_distill
from src.knowledge.store import get_store
from src.notify import alert
from src.pipeline import TASK_REGISTRY
from src.tailor.resume_tailor import ensure_tailored_pdf, tailor_resume_for_job

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

logger = logging.getLogger(__name__)

# In-memory map of active edit job per chat
_ACTIVE_EDIT_JOB: dict[int, int] = {}

# In-memory deduplication of update IDs
_PROCESSED_UPDATE_IDS = collections.deque(maxlen=1000)

_ADVISORY_LOCK_CONN = None
_JOB_LOCKS: dict[int, asyncio.Lock] = {}


def _get_job_lock(job_id: int) -> asyncio.Lock:
    if job_id not in _JOB_LOCKS:
        _JOB_LOCKS[job_id] = asyncio.Lock()
    return _JOB_LOCKS[job_id]


def acquire_bot_lock() -> bool:
    """Acquires exclusive, OS-independent PostgreSQL session advisory lock."""
    global _ADVISORY_LOCK_CONN
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return True
    conn = try_acquire_advisory_lock()
    if conn:
        _ADVISORY_LOCK_CONN = conn
        return True
    return False


def get_active_bot_pid() -> str:
    return "PostgreSQL Advisory Lock Active"


def release_bot_lock():
    """Releases the OS-independent advisory lock connection."""
    global _ADVISORY_LOCK_CONN
    if _ADVISORY_LOCK_CONN:
        try:
            _ADVISORY_LOCK_CONN.close()
        except Exception:
            pass
        _ADVISORY_LOCK_CONN = None


class InlineKeyboardList(list):
    def __getitem__(self, item):
        if item == "inline_keyboard":
            return self
        return super().__getitem__(item)


def build_inline_keyboard(job_id: int) -> list[list[dict]]:
    """Builds discovery buttons for new job cards."""
    return InlineKeyboardList([
        [
            {"text": "🎯 Interested (Tailor & Check Site)", "callback_data": f"interest:{job_id}"},
            {"text": "👎 Skip: Bad Fit", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"},
        ],
        [
            {"text": "🚫 Skip: Ghost/Scam", "callback_data": f"act:SKIP_SCAM:{job_id}"},
            {"text": "⭐ Got Callback!", "callback_data": f"out:CALLBACK:{job_id}"},
        ],
    ])


async def cleanup_job_messages(
    client: httpx.AsyncClient, chat_id: int, job_id: int, extra_mid: int | None = None
) -> None:
    """Deletes all active messages for this job from Telegram to guarantee 1-card lifecycle."""
    def _get_active():
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
            return []
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT message_id FROM telegram_message_jobs WHERE job_id = %s AND chat_id = %s AND is_active = TRUE;",
                (job_id, chat_id),
            )
            return [r["message_id"] for r in cur.fetchall()]

    try:
        active_mids = await asyncio.to_thread(_get_active)
    except Exception:
        active_mids = []

    mids_to_del = set(active_mids)
    if extra_mid:
        mids_to_del.add(extra_mid)

    for mid in mids_to_del:
        try:
            await client.post(
                f"{TG_BASE}/deleteMessage",
                json={"chat_id": chat_id, "message_id": mid},
                timeout=5.0,
            )
        except Exception:
            pass

    def _mark_inactive():
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
            return
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE telegram_message_jobs SET is_active = FALSE WHERE job_id = %s AND chat_id = %s;",
                (job_id, chat_id),
            )
            conn.commit()

    try:
        await asyncio.to_thread(_mark_inactive)
    except Exception:
        pass


async def upsert_job_rich_card(
    client: httpx.AsyncClient,
    chat_id: int,
    job_id: int,
    markdown_body: str,
    inline_keyboard: list[list[dict]] | None = None,
    screenshot_path: str | None = None,
    pdf_path: str | None = None,
    try_edit_text_only: bool = False,
    clicked_mid: int | None = None,
) -> int | None:
    """
    Telegram Bot API 10.3 sendRichMessage & editMessageText:
    Guarantees exactly ONE active message in the DM for this job_id at all times.
    Bundles rich markdown, embedded photo preview, downloadable PDF, and inline keyboard.
    """
    async with _get_job_lock(job_id):
        # Path 1: Fast text-only in-place update for progress states
        if try_edit_text_only and not screenshot_path and not pdf_path:
            def _find_active_mid():
                if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
                    return None
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT message_id FROM telegram_message_jobs WHERE job_id = %s AND chat_id = %s AND is_active = TRUE ORDER BY id DESC LIMIT 1;",
                        (job_id, chat_id),
                    )
                    row = cur.fetchone()
                    return row["message_id"] if row else None

            try:
                active_mid = await asyncio.to_thread(_find_active_mid)
            except Exception:
                active_mid = None

            target_mid = clicked_mid or active_mid
            if target_mid:
                try:
                    payload = {
                        "chat_id": chat_id,
                        "message_id": target_mid,
                        "rich_message": {"markdown": markdown_body},
                        "reply_markup": {"inline_keyboard": inline_keyboard or []},
                    }
                    resp = await client.post(f"{TG_BASE}/editMessageText", json=payload, timeout=10.0)
                    if resp.status_code == 200:
                        return target_mid
                except Exception:
                    pass

        # Path 2: sendRichMessage (deletes previous cards and creates 1 fresh unified card)
        await cleanup_job_messages(client, chat_id, job_id, extra_mid=clicked_mid)

        media_items = []
        files_payload = {}

        if screenshot_path and Path(screenshot_path).exists():
            media_items.append({
                "id": "form_shot",
                "media": {"type": "photo", "media": "attach://form_shot_file"},
            })
            files_payload["form_shot_file"] = (
                Path(screenshot_path).name,
                Path(screenshot_path).read_bytes(),
                "image/png",
            )

        if pdf_path and Path(pdf_path).exists():
            media_items.append({
                "id": "resume_pdf",
                "media": {"type": "document", "media": "attach://resume_pdf_file"},
            })
            files_payload["resume_pdf_file"] = (
                Path(pdf_path).name,
                Path(pdf_path).read_bytes(),
                "application/pdf",
            )

        rich_msg_obj = {"markdown": markdown_body}
        if media_items:
            rich_msg_obj["media"] = media_items

        try:
            if files_payload:
                data = {"chat_id": str(chat_id), "rich_message": json.dumps(rich_msg_obj)}
                if inline_keyboard is not None:
                    data["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard})
                resp = await client.post(f"{TG_BASE}/sendRichMessage", data=data, files=files_payload, timeout=60.0)
            else:
                payload = {"chat_id": chat_id, "rich_message": rich_msg_obj}
                if inline_keyboard is not None:
                    payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
                resp = await client.post(f"{TG_BASE}/sendRichMessage", json=payload, timeout=15.0)

            if resp.status_code == 200:
                mid = resp.json().get("result", {}).get("message_id")
                if mid:
                    def _record_active():
                        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
                            return
                        with get_connection() as conn, conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO telegram_message_jobs (message_id, chat_id, job_id, is_active)
                                VALUES (%s, %s, %s, TRUE)
                                ON CONFLICT (message_id) DO UPDATE SET job_id = EXCLUDED.job_id, is_active = TRUE;
                                """,
                                (mid, chat_id, job_id),
                            )
                            cur.execute(
                                "UPDATE jobs SET last_telegram_message_id = %s WHERE id = %s;",
                                (mid, job_id),
                            )
                            conn.commit()

                    await asyncio.to_thread(_record_active)
                    return mid
            else:
                logger.warning("sendRichMessage returned %s: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("Error sending rich message for job #%s: %s", job_id, e)
        return None


def _find_job_for_message(reply_msg: dict) -> int | None:
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
    full_text = f"{reply_msg.get('text', '')} {reply_msg.get('caption', '')}"
    m = _re.search(r"Job\s*#(\d+)", full_text, _re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _append_user_edit_note(job_id: int, note: str, chat_id: int | None = None) -> None:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET user_edit_notes = COALESCE(user_edit_notes, '[]'::jsonb) || %s::jsonb,
                    workflow_stage = 'EDITING',
                    active_chat_id = COALESCE(%s, active_chat_id)
                WHERE id = %s;
                """,
                (json.dumps([note]), chat_id, job_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Could not append user edit note: %s", e)


async def send_single_job_card(j: dict, chat_id: int | None = None) -> int | None:
    raw_chat = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip() or CHAT_ID
    try:
        target_chat = int(raw_chat)
    except (ValueError, TypeError):
        target_chat = 0
    jid = j.get("id")
    if not jid:
        logger.warning("Cannot send single job card: missing job id in %s", j)
        return None

    # Defensively hydrate from database if critical fields are missing
    if not j.get("apply_url") or not j.get("company"):
        def _hydrate():
            try:
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute("SELECT * FROM jobs WHERE id = %s;", (jid,))
                    row = cur.fetchone()
                    if row:
                        for k, v in dict(row).items():
                            if not j.get(k) and v is not None:
                                j[k] = v
            except Exception as e:
                logger.warning("Could not hydrate missing job fields from DB for job #%s: %s", jid, e)

        await asyncio.to_thread(_hydrate)

    company = j.get("company", "Unknown")
    title = j.get("title", "Untitled")
    location = j.get("location", "Remote")
    source_ats = j.get("source_ats", "web")
    apply_url = j.get("apply_url") or ""

    effective_fit = j.get("jev_fit_prob_cal") or j.get("jev_fit_prob") or 0.0
    fit_pct = int(effective_fit * 100)
    scam_pct = int((j.get("jev_scam_prob") or 0) * 100)

    apply_link_md = f"🔗 [Open Direct Application Page]({apply_url})" if apply_url else "🔗 *(Apply URL not available)*"

    markdown = (
        f"🎯 *{company}* — *{title}* (Job #{jid})\n"
        f"📍 `{location}` | 🏢 ATS: `{source_ats}`\n\n"
        f"🧠 *System-1 Jev Triage:*\n"
        f"• Fit Probability: *{fit_pct}%* (`{j.get('jev_fit_tier', 'fit')}`)\n"
        f"• Scam/Ghost Risk: *{scam_pct}%*\n"
        f"• Baseline Sim: `{(j.get('cosine_sim') or 0):.2f}`\n"
        f"• Persona Route: `{j.get('jev_persona', 'general')}`\n\n"
        f"{apply_link_md}"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        mid = await upsert_job_rich_card(
            client=client,
            chat_id=target_chat,
            job_id=jid,
            markdown_body=markdown,
            inline_keyboard=build_inline_keyboard(jid),
        )

        if mid:
            def _mark_notified():
                with get_connection() as c, c.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET notified_at = NOW(), workflow_stage = 'NOTIFIED', active_chat_id = %s WHERE id = %s;",
                        (target_chat, jid),
                    )
                    c.commit()

            await asyncio.to_thread(_mark_notified)
            print(f"📲 Sent single rich card for [{company}] {title} (Job #{jid})", flush=True)
            return mid
    return None


async def send_pending_digests(limit: int = 5):
    """Sends newly triaged, unnotified jobs to Telegram via sendRichMessage."""
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    def _fetch_pending():
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
            return [dict(r) for r in cur.fetchall()]

    try:
        jobs = await asyncio.to_thread(_fetch_pending)
    except Exception as e:
        logger.warning("Could not fetch pending digests: %s", e)
        return

    if not jobs:
        print("📭 No unnotified triaged jobs to send right now.", flush=True)
        return

    for j in jobs:
        await send_single_job_card(j)
        await asyncio.sleep(0.4)


async def _process_job_interest(
    job_row: dict,
    chat_id: int,
    custom_note: str = "",
    is_edit: bool = False,
    clicked_mid: int | None = None,
):
    """
    Executes ATS Resume Tailoring + Form Inspection under Playwright semaphore,
    delivering strictly ONE single Telegram Rich Message (photo + PDF + buttons).
    """
    job_id = job_row["id"]
    company = job_row["company"]
    title = job_row["title"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Step 1: Progress state
        progress_text_1 = (
            f"⏳ *Job #{job_id} ({company} — {title}) — Under Process*\n\n"
            f"*(Step 1/2: Tailoring ATS PDF with zero hallucination...)*"
        )
        await upsert_job_rich_card(
            client=client,
            chat_id=chat_id,
            job_id=job_id,
            markdown_body=progress_text_1,
            try_edit_text_only=True,
            clicked_mid=clicked_mid,
        )

        TASK_REGISTRY.set_job_stage(job_id, "Tailoring ATS PDF")
        try:
            tailor_res = await tailor_resume_for_job(job_row, custom_instruction=custom_note)
            pdf_path = tailor_res["pdf_path"]
            pdf_sha = tailor_res["pdf_sha256"]
            diff_summary = tailor_res["diff_summary"]

            job_row["tailored_pdf_path"] = pdf_path
            job_row["tailored_pdf_sha256"] = pdf_sha

            # Step 2: Progress state
            progress_text_2 = (
                f"⏳ *Job #{job_id} ({company} — {title}) — Under Process*\n\n"
                f"*(Step 2/2: Inspecting & pre-filling application website...)*"
            )
            await upsert_job_rich_card(
                client=client,
                chat_id=chat_id,
                job_id=job_id,
                markdown_body=progress_text_2,
                try_edit_text_only=True,
                clicked_mid=clicked_mid,
            )

            TASK_REGISTRY.set_job_stage(job_id, "Inspecting application website")
            try:
                site_res = await prepare_application(job_row)
            except Exception as e:
                logger.error("Site preparation error: %s", e)
                site_res = {"status": "FAILED", "error": str(e), "feasible": False, "screenshots": []}

            feasible = bool(site_res.get("feasible", False))
            blocker = site_res.get("error") or ("Direct form ready" if feasible else "Blocked")
            screenshots = site_res.get("screenshots") or []

            evidence_shots = [s for s in screenshots if Path(s).exists()]
            evidence_img = evidence_shots[-1] if evidence_shots else None

            # Build unified Rich Review Card
            site_status_line = (
                "🟢 *Website Check:* Direct application form detected & pre-filled! Ready for 1-tap auto-submit."
                if feasible
                else f"🟡 *Website Check:* Auto-submit blocked ({blocker}). Use the direct link below to submit your tailored PDF manually!"
            )

            edit_header = (
                f"👋 *Hey! For Job #{job_id} ({company} — {title}), here is your updated PDF:*\n"
                f"*Requested Changes:* \"{custom_note}\"\n\n"
                if is_edit
                else f"🎯 *Tailored Application Package — Job #{job_id}*\n*{company}* — *{title}*\n\n"
            )

            media_links = []
            if evidence_img:
                media_links.append("[📸 Application Form Preview](tg://photo?id=form_shot)")
            if pdf_path and Path(pdf_path).exists():
                media_links.append("[📄 Download Tailored Resume PDF](tg://document?id=resume_pdf)")
            media_links.append(f"[🌐 Open Employer Application Page]({job_row['apply_url']})")

            media_line = " | ".join(media_links)

            markdown_card = (
                f"{edit_header}"
                f"{site_status_line}\n\n"
                f"{media_line}\n\n"
                f"{diff_summary}"
            )

            buttons = []
            if feasible:
                def _create_token():
                    return create_apply_confirmation(
                        job_id,
                        kind="SUBMIT",
                        payload={"pdf_sha256": pdf_sha, "apply_url": job_row["apply_url"]},
                        ttl_seconds=1800,
                        created_by=str(chat_id),
                    )

                raw_token, _ = await asyncio.to_thread(_create_token)
                buttons.append([{"text": "🚀 Auto-Submit Now", "callback_data": f"submit:{job_id}:{raw_token}"}])

            buttons.append([
                {"text": "✏️ Request Resume Changes", "callback_data": f"edit_prompt:{job_id}"},
                {"text": "✅ Mark Applied Manually", "callback_data": f"manual_applied:{job_id}"},
            ])
            buttons.append([
                {"text": "🔄 Re-check Website", "callback_data": f"prepare:{job_id}"},
                {"text": "❌ Drop Job", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"},
            ])

            await upsert_job_rich_card(
                client=client,
                chat_id=chat_id,
                job_id=job_id,
                markdown_body=markdown_card,
                inline_keyboard=buttons,
                screenshot_path=evidence_img,
                pdf_path=pdf_path,
                clicked_mid=clicked_mid,
            )

            def _mark_ready():
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET workflow_stage = 'READY_FOR_REVIEW', active_chat_id = %s WHERE id = %s;",
                        (chat_id, job_id),
                    )
                    conn.commit()

            await asyncio.to_thread(_mark_ready)

        except Exception as e:
            logger.error("Error processing interest for job #%s: %s", job_id, e)
            err_markdown = f"⚠️ *Job #{job_id} ({company}):* Processing failed: `{e}`"
            await upsert_job_rich_card(
                client=client,
                chat_id=chat_id,
                job_id=job_id,
                markdown_body=err_markdown,
                inline_keyboard=[[{"text": "🔄 Retry", "callback_data": f"interest:{job_id}"}]],
                clicked_mid=clicked_mid,
            )
        finally:
            TASK_REGISTRY.clear_job_stage(job_id)


async def resume_job_interest_after_crash(job_row: dict, custom_note: str = "", is_edit: bool = False):
    """Crash Recovery: Restores an interrupted PROCESSING or EDITING job."""
    chat_id = job_row.get("active_chat_id") or int(CHAT_ID)
    job_id = job_row["id"]
    async with httpx.AsyncClient(timeout=15.0) as client:
        await upsert_job_rich_card(
            client=client,
            chat_id=chat_id,
            job_id=job_id,
            markdown_body=f"⏳ *Job #{job_id} ({job_row['company']})* — Resuming resume tailoring & website check after restart...",
            try_edit_text_only=True,
        )
    await _process_job_interest(job_row, chat_id, custom_note=custom_note, is_edit=is_edit)


async def resume_job_submit_after_crash(job_row: dict):
    """Crash Recovery: Safely checks submission status of an interrupted SUBMITTING job."""
    job_id = job_row["id"]
    chat_id = job_row.get("active_chat_id") or int(CHAT_ID)

    def _check_already_submitted():
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM apply_events WHERE job_id = %s AND status = 'SUBMITTED';",
                (job_id,),
            )
            return cur.fetchone() is not None

    try:
        already_submitted = await asyncio.to_thread(_check_already_submitted)
    except Exception:
        already_submitted = False

    async with httpx.AsyncClient(timeout=30.0) as client:
        if already_submitted:
            def _mark_app():
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET workflow_stage = 'APPLIED', apply_status = 'SUBMITTED' WHERE id = %s;",
                        (job_id,),
                    )
                    conn.commit()

            await asyncio.to_thread(_mark_app)
            await upsert_job_rich_card(
                client=client,
                chat_id=chat_id,
                job_id=job_id,
                markdown_body=f"✅ *Job #{job_id} ({job_row['company']})* — Verified application submitted successfully before restart!",
            )
        else:
            # Re-offer submit button safely without double-submitting
            pdf_path = await ensure_tailored_pdf(job_row)
            pdf_sha = job_row.get("tailored_pdf_sha256")
            raw_token, _ = await asyncio.to_thread(
                create_apply_confirmation,
                job_id,
                kind="SUBMIT",
                payload={"pdf_sha256": pdf_sha, "apply_url": job_row["apply_url"]},
                ttl_seconds=1800,
                created_by=str(chat_id),
            )

            warn_markdown = (
                f"⚠️ *Job #{job_id} ({job_row['company']} — {job_row['title']})*\n"
                f"The bot restarted while submitting. Please review and tap below to submit safely:\n\n"
                f"🔗 [Direct Application Link]({job_row['apply_url']})"
            )
            buttons = [
                [{"text": "🚀 Auto-Submit Now", "callback_data": f"submit:{job_id}:{raw_token}"}],
                [{"text": "✅ Mark Applied Manually", "callback_data": f"manual_applied:{job_id}"}],
                [{"text": "❌ Drop Job", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"}],
            ]

            def _reset_stage():
                with get_connection() as conn, conn.cursor() as cur:
                    cur.execute("UPDATE jobs SET workflow_stage = 'READY_FOR_REVIEW' WHERE id = %s;", (job_id,))
                    conn.commit()

            await asyncio.to_thread(_reset_stage)
            await upsert_job_rich_card(
                client=client,
                chat_id=chat_id,
                job_id=job_id,
                markdown_body=warn_markdown,
                inline_keyboard=buttons,
                pdf_path=pdf_path,
            )


async def handle_slash_command(client: httpx.AsyncClient, cmd_text: str, chat_id=None):
    if chat_id is None:
        chat_id = CHAT_ID
    cmd = cmd_text.strip().split()[0].lower()

    if cmd in ("/start", "/help"):
        reply = (
            "🤖 *Relentless v2.0 Control Center*\n\n"
            "• `/digest` — Send top unreviewed job matches\n"
            "• `/status` — Check database, live pipelines & tasks\n"
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
        def _get_counts():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM jobs;")
                total = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE triage_passed = TRUE;")
                passed = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE human_action IS NOT NULL;")
                labeled = cur.fetchone()["c"]
                return total, passed, labeled

        total, passed, labeled = await asyncio.to_thread(_get_counts)
        reg_status = TASK_REGISTRY.get_all_status()
        active_jobs = reg_status.get("active_jobs", {})
        sys_tasks = reg_status.get("system_tasks", [])

        active_jobs_lines = (
            "\n".join(f"  • Job #{jid}: {desc}" for jid, desc in active_jobs.items())
            if active_jobs
            else "  • Idle (0 jobs in flight)"
        )
        sys_tasks_lines = (
            ", ".join(sys_tasks) if sys_tasks else "All producers idle / between cycles"
        )

        reply = (
            f"📊 *Relentless Streaming Status*\n"
            f"• Total Scraped Jobs: `{total}`\n"
            f"• Passed System-1 Triage: `{passed}`\n"
            f"• Human Labeled (`n`): `{labeled}`\n\n"
            f"⚙️ *Live Tasks:*\n{active_jobs_lines}\n"
            f"🌐 *Active Producers:* {sys_tasks_lines}"
        )
    elif cmd == "/brain":
        store = get_store()
        await asyncio.to_thread(store.hydrate)
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
        reply = await asyncio.to_thread(run_evaluation)
    elif cmd in ("/reset_labels", "/reset_triage", "/wipe_all"):
        mode_map = {"/reset_labels": "labels", "/reset_triage": "triage", "/wipe_all": "all"}
        mode = mode_map[cmd]
        raw_token, _expires = await asyncio.to_thread(
            create_pending_action,
            "DESTRUCTIVE",
            {"mode": mode, "chat_id": int(chat_id) if chat_id else None},
            ttl_seconds=600,
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

        def _pause():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('automation_paused_until', %s, NOW()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
                    (Jsonb(until.isoformat()),),
                )
                conn.commit()

        await asyncio.to_thread(_pause)
        reply = f"⏸️ Auto-apply paused until {until.isoformat()}"
    elif cmd == "/apply_status":
        def _get_apply_events():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, status, router_path, started_at FROM apply_events ORDER BY started_at DESC LIMIT 10;"
                )
                return cur.fetchall()

        rows = await asyncio.to_thread(_get_apply_events)
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


async def _handle_single_update(client: httpx.AsyncClient, upd: dict) -> None:
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
        return

    chat_id = update_chat_id(upd) or int(CHAT_ID)

    # 1. Handle text messages / replies / instructions
    msg_obj = upd.get("message")
    if msg_obj and isinstance(msg_obj, dict):
        text_in = msg_obj.get("text", "")
        if isinstance(text_in, str) and text_in.strip():
            if text_in.startswith("/"):
                await handle_slash_command(client, text_in, chat_id=chat_id)
                return

            resolved_jid = None
            edit_instruction = ""

            # Method 1: #<id> <instruction>
            if text_in.strip().startswith("#"):
                m = _re.match(r"^#(\d+)\s*(.*)$", text_in.strip(), _re.DOTALL)
                if m:
                    resolved_jid = int(m.group(1))
                    edit_instruction = m.group(2).strip()

            # Method 2: Reply to a bot message
            elif msg_obj.get("reply_to_message"):
                resolved_jid = _find_job_for_message(msg_obj["reply_to_message"])
                edit_instruction = text_in.strip()

            # Method 3: Active edit session
            elif chat_id in _ACTIVE_EDIT_JOB:
                resolved_jid = _ACTIVE_EDIT_JOB[chat_id]
                edit_instruction = text_in.strip()

            # Method 4: Fallback to job with AWAITING_EDIT_NOTE in DB
            if not resolved_jid:
                def _find_awaiting():
                    with get_connection() as conn, conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM jobs WHERE workflow_stage = 'AWAITING_EDIT_NOTE' ORDER BY id DESC LIMIT 1;"
                        )
                        r = cur.fetchone()
                        return r["id"] if r else None

                try:
                    resolved_jid = await asyncio.to_thread(_find_awaiting)
                    edit_instruction = text_in.strip()
                except Exception:
                    pass

            if resolved_jid:
                # Fast in-flight check
                in_flight_desc = TASK_REGISTRY.get_job_stage(resolved_jid)
                if in_flight_desc:
                    await client.post(
                        f"{TG_BASE}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": (
                                f"⏳ Job #{resolved_jid} is currently in process ({in_flight_desc}). "
                                "Please wait a moment — this card will update as soon as it's done!"
                            ),
                        },
                    )
                    return

                def _fetch_jr():
                    with get_connection() as conn, conn.cursor() as cur:
                        cur.execute("SELECT * FROM jobs WHERE id = %s;", (resolved_jid,))
                        r = cur.fetchone()
                        return dict(r) if r else None

                try:
                    jr = await asyncio.to_thread(_fetch_jr)
                    if jr:
                        # Write-through commit first
                        await asyncio.to_thread(_append_user_edit_note, resolved_jid, edit_instruction, chat_id)
                        _ACTIVE_EDIT_JOB.pop(chat_id, None)

                        # Update card in place to indicate revision is under way
                        await upsert_job_rich_card(
                            client=client,
                            chat_id=chat_id,
                            job_id=resolved_jid,
                            markdown_body=(
                                f"⏳ *Job #{resolved_jid} ({jr['company']} — {jr['title']}) — Under Process*\n\n"
                                f"Re-tailoring your resume with: \"{edit_instruction}\"..."
                            ),
                            try_edit_text_only=True,
                        )

                        # Trigger OKF distill
                        asyncio.create_task(asyncio.to_thread(run_distill))

                        # Process revision
                        asyncio.create_task(
                            _process_job_interest(jr, chat_id, custom_note=edit_instruction, is_edit=True)
                        )
                except Exception as e:
                    logger.error("Error processing user edit note for job %s: %s", resolved_jid, e)
                return

    # 2. Handle button callbacks
    cb = upd.get("callback_query")
    if not cb:
        return

    cb_id = cb["id"]
    payload = cb.get("data", "")
    cb_msg = cb.get("message") or {}
    clicked_mid = cb_msg.get("message_id")

    # Confirm/cancel destructive actions
    if payload.startswith(("confirm:", "cancel:")):
        token = payload.split(":", 1)[1]
        if payload.startswith("cancel:"):
            await asyncio.to_thread(delete_pending_action, token)
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": "Cancelled"},
            )
            return
        row = await asyncio.to_thread(consume_pending_action, token, kind="DESTRUCTIVE")
        if not row:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": "Invalid/expired token"},
            )
            return
        payload_data = row["payload"] or {}
        mode = payload_data.get("mode", "labels")
        mode_to_reset = {"labels": "labels", "triage": "triage", "all": "all"}[mode]
        try:
            msg = await asyncio.to_thread(reset_database, mode_to_reset)
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
        return

    # Interested (tailor & check site)
    if payload.startswith(("interest:", "act:APPLIED:")):
        try:
            job_id = int(payload.split(":")[-1])
        except ValueError:
            return

        # Busy check (<100ms)
        in_flight_desc = TASK_REGISTRY.get_job_stage(job_id)
        if in_flight_desc:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={
                    "callback_query_id": cb_id,
                    "text": f"⏳ Job #{job_id} is already in process ({in_flight_desc}).",
                    "show_alert": True,
                },
            )
            return

        await client.post(
            f"{TG_BASE}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": "Starting tailoring & website inspection..."},
        )

        def _mark_processing():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET human_action = 'APPLIED',
                        human_action_at = NOW(),
                        workflow_stage = 'PROCESSING',
                        active_chat_id = %s
                    WHERE id = %s
                    RETURNING *;
                    """,
                    (chat_id, job_id),
                )
                r = cur.fetchone()
                conn.commit()
                return dict(r) if r else None

        job_row = await asyncio.to_thread(_mark_processing)
        if job_row:
            asyncio.create_task(_process_job_interest(job_row, chat_id, clicked_mid=clicked_mid))
            asyncio.create_task(asyncio.to_thread(run_distill))
        return

    # Edit Prompt requested
    if payload.startswith("edit_prompt:"):
        try:
            job_id = int(payload.split(":", 1)[1])
        except ValueError:
            return

        _ACTIVE_EDIT_JOB[chat_id] = job_id

        def _mark_awaiting():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET workflow_stage = 'AWAITING_EDIT_NOTE', active_chat_id = %s WHERE id = %s RETURNING company, title;",
                    (chat_id, job_id),
                )
                r = cur.fetchone()
                conn.commit()
                return dict(r) if r else None

        jr = await asyncio.to_thread(_mark_awaiting)
        co_name = jr["company"] if jr else f"Job #{job_id}"

        await client.post(
            f"{TG_BASE}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": "Type your resume changes:"},
        )

        prompt_card = (
            f"✏️ *Request Resume Changes for Job #{job_id} ({co_name})*\n\n"
            f"Send me your instructions in your next message:\n"
            f"• Example: `Emphasize LangGraph agent workflows and add FastAPI microservice optimization`\n\n"
            f"*(Tap Cancel below to restore your current review card at any time)*"
        )
        buttons = [[{"text": "❌ Cancel Edit", "callback_data": f"cancel_edit:{job_id}"}]]
        await upsert_job_rich_card(
            client=client,
            chat_id=chat_id,
            job_id=job_id,
            markdown_body=prompt_card,
            inline_keyboard=buttons,
            try_edit_text_only=True,
            clicked_mid=clicked_mid,
        )
        return

    # Cancel Edit
    if payload.startswith("cancel_edit:"):
        try:
            job_id = int(payload.split(":", 1)[1])
        except ValueError:
            return
        _ACTIVE_EDIT_JOB.pop(chat_id, None)

        def _fetch_restore():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET workflow_stage = 'READY_FOR_REVIEW' WHERE id = %s RETURNING *;",
                    (job_id,),
                )
                r = cur.fetchone()
                conn.commit()
                return dict(r) if r else None

        job_row = await asyncio.to_thread(_fetch_restore)
        if job_row:
            asyncio.create_task(resume_job_interest_after_crash(job_row, is_edit=False))
        await client.post(
            f"{TG_BASE}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": "Edit cancelled."},
        )
        return

    # Mark Applied Manually
    if payload.startswith("manual_applied:"):
        try:
            job_id = int(payload.split(":", 1)[1])
        except ValueError:
            return

        def _mark_manual():
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
                r = cur.fetchone()
                conn.commit()
                return dict(r) if r else None

        jr = await asyncio.to_thread(_mark_manual)
        co_name = jr["company"] if jr else f"Job #{job_id}"

        await client.post(
            f"{TG_BASE}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": "Marked Applied!"},
        )
        await upsert_job_rich_card(
            client=client,
            chat_id=chat_id,
            job_id=job_id,
            markdown_body=f"🎉 *Job #{job_id} ({co_name})* — Marked as applied manually. Best of luck!",
            try_edit_text_only=True,
            clicked_mid=clicked_mid,
        )
        asyncio.create_task(asyncio.to_thread(run_distill))
        return

    # Submit Application
    if payload.startswith("submit:"):
        parts = payload.split(":", 2)
        if len(parts) != 3:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": "Bad payload"},
            )
            return

        job_id = int(parts[1])
        token = parts[2]

        in_flight_desc = TASK_REGISTRY.get_job_stage(job_id)
        if in_flight_desc:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": f"Job #{job_id} is busy ({in_flight_desc}).", "show_alert": True},
            )
            return

        def _set_submitting():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET workflow_stage = 'SUBMITTING', active_chat_id = %s WHERE id = %s RETURNING *;",
                    (chat_id, job_id),
                )
                r = cur.fetchone()
                conn.commit()
                return dict(r) if r else None

        job_row = await asyncio.to_thread(_set_submitting)
        if not job_row:
            return

        await client.post(
            f"{TG_BASE}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": "Submitting application..."},
        )

        await upsert_job_rich_card(
            client=client,
            chat_id=chat_id,
            job_id=job_id,
            markdown_body=f"⏳ *Job #{job_id} ({job_row['company']}) — Under Process: Submitting application...*",
            try_edit_text_only=True,
            clicked_mid=clicked_mid,
        )

        async def _run_submission():
            TASK_REGISTRY.set_job_stage(job_id, "Submitting application via browser")
            try:
                res = await submit_application(job_row, token)
                st = res.get("status")
                err = res.get("error") or ""

                is_success = st == "SUBMITTED"
                status_icon = "🚀" if is_success else "⚠️"
                result_text = (
                    f"{status_icon} *Auto-Submit Result for Job #{job_id} ({job_row['company']}):* *{st}*\n\n"
                    f"{err}\n"
                    f"🔗 [Application Page]({job_row['apply_url']})"
                )

                sub_shots = [s for s in (res.get("screenshots") or []) if Path(s).exists()]
                proof_img = sub_shots[-1] if sub_shots else None
                pdf_p = await ensure_tailored_pdf(job_row)

                buttons = []
                if not is_success:
                    buttons.append([{"text": "🔄 Retry / Review", "callback_data": f"interest:{job_id}"}])
                    buttons.append([{"text": "✅ Mark Applied Manually", "callback_data": f"manual_applied:{job_id}"}])

                def _mark_done():
                    with get_connection() as conn, conn.cursor() as cur:
                        final_stage = "APPLIED" if is_success else "READY_FOR_REVIEW"
                        cur.execute(
                            "UPDATE jobs SET workflow_stage = %s WHERE id = %s;",
                            (final_stage, job_id),
                        )
                        conn.commit()

                await asyncio.to_thread(_mark_done)
                await upsert_job_rich_card(
                    client=client,
                    chat_id=chat_id,
                    job_id=job_id,
                    markdown_body=result_text,
                    inline_keyboard=buttons if buttons else None,
                    screenshot_path=proof_img,
                    pdf_path=pdf_p,
                )
            finally:
                TASK_REGISTRY.clear_job_stage(job_id)

        asyncio.create_task(_run_submission())
        return

    # Re-check Website
    if payload.startswith("prepare:"):
        try:
            job_id = int(payload.split(":", 1)[1])
        except ValueError:
            return

        def _fetch_recheck():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
                r = cur.fetchone()
                return dict(r) if r else None

        job_row = await asyncio.to_thread(_fetch_recheck)
        if job_row:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": "Re-analyzing website..."},
            )
            asyncio.create_task(_process_job_interest(job_row, chat_id, clicked_mid=clicked_mid))
        return

    # Skip Bad Fit / Skip Scam / Drop Job / Callback
    parts = payload.split(":")
    if len(parts) == 3:
        kind, label, job_id_str = parts[0], parts[1], parts[2]
        try:
            job_id = int(job_id_str)
        except ValueError:
            return

        def _log_label():
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
                r = cur.fetchone()
                conn.commit()
                return dict(r) if r else None

        await asyncio.to_thread(_log_label)

        if "SKIP" in label:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": "🗑️ Skipped!"},
            )
            # Complete deletion on Skip per spec (0 messages left in DM)
            await cleanup_job_messages(client, chat_id, job_id, extra_mid=clicked_mid)
        else:
            await client.post(
                f"{TG_BASE}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": f"Logged: {label}!"},
            )
            await upsert_job_rich_card(
                client=client,
                chat_id=chat_id,
                job_id=job_id,
                markdown_body=f"⭐ *Job #{job_id}* marked as: `{label}`!",
                try_edit_text_only=True,
                clicked_mid=clicked_mid,
            )

        print(f"✅ Ground-Truth Logged -> Job #{job_id}: {label}", flush=True)
        asyncio.create_task(asyncio.to_thread(run_distill))


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

                    # Non-blocking dispatch so long tasks never freeze getUpdates
                    asyncio.create_task(_handle_single_update(client, upd))

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
        print(
            "⚠️ Telegram bot listener is already running in another session (PostgreSQL session advisory lock held). "
            "Waiting for existing instance to exit before taking over...",
            flush=True,
        )
        while not acquire_bot_lock():
            await asyncio.sleep(5)
        print("🎧 Acquired Telegram bot advisory lock. Starting listener...", flush=True)

    print("🎧 Listening for Telegram button taps, replies & /commands (/help, /brain, /status)...", flush=True)
    try:
        await _listen_loop()
    finally:
        release_bot_lock()


async def main():
    await send_pending_digests(limit=3)
    await listen_for_button_clicks()


if __name__ == "__main__":
    asyncio.run(main())
