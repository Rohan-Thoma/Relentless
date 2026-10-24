import asyncio
import os

import httpx
from dotenv import load_dotenv

from evals.evaluate_triage import run_evaluation
from datetime import UTC, datetime, timedelta
from psycopg.types.json import Jsonb
import re as _re
import logging

from src.db import get_connection, reset_database, consume_pending_action, delete_pending_action, create_pending_action, create_apply_confirmation
from src.hitl.auth import is_authorized, update_chat_id
from src.orchestrator.graph import RELENTLESS_GRAPH
from src.apply.runner import prepare_application, submit_application, attempt_apply
from src.notify import alert

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def build_inline_keyboard(job_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Apply / Tailor PDF", "callback_data": f"act:APPLIED:{job_id}"},
                {"text": "👎 Skip: Bad Fit", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"},
            ],
            [
                {"text": "🚫 Skip: Ghost/Scam", "callback_data": f"act:SKIP_SCAM:{job_id}"},
                {"text": "⭐ Got Callback!", "callback_data": f"out:CALLBACK:{job_id}"},
            ],
            [
                {"text": "🛠️ Prepare Application", "callback_data": f"prepare:{job_id}"},
            ],
        ]
    }

async def send_pending_digests(limit: int = 5):
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT id, source_ats, company, title, location, apply_url,
                       jev_fit_prob, jev_fit_tier, jev_scam_prob, jev_persona, cosine_sim
                FROM jobs
                WHERE triage_passed = TRUE
                  AND human_action IS NULL
                ORDER BY jev_fit_prob DESC
                LIMIT %s;
                """,
            (limit,),
        )
        jobs = cur.fetchall()

    if not jobs:
        print("📭 No unreviewed triaged jobs to send right now.")
        return

    async with httpx.AsyncClient(timeout=15.0) as client:
        for j in jobs:
            fit_pct = int((j["jev_fit_prob"] or 0) * 100)
            scam_pct = int((j["jev_scam_prob"] or 0) * 100)
            text = (
                f"🎯 *{j['company']}* — *{j['title']}*\n"
                f"📍 `{j['location']}` | 🏢 ATS: `{j['source_ats']}`\n\n"
                f"🧠 *System-1 Jev Triage:*\n"
                f"• Fit Probability: *{fit_pct}%* (`{j['jev_fit_tier']}`)\n"
                f"• Scam/Ghost Risk: *{scam_pct}%*\n"
                f"• Baseline Sim: `{(j['cosine_sim'] or 0):.2f}`\n"
                f"• Persona Route: `{j['jev_persona']}`\n\n"
                f"🔗 [Open Direct Application Page]({j['apply_url']})"
            )
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
                print(f"📲 Sent Telegram card for [{j['company']}] {j['title']} (Job #{j['id']})")
            await asyncio.sleep(0.4)

async def handle_slash_command(client: httpx.AsyncClient, cmd_text: str, chat_id=None):
    if chat_id is None:
        chat_id = CHAT_ID
    """Handles remote management and reset commands sent directly from your Telegram phone chat."""
    cmd = cmd_text.strip().split()[0].lower()

    if cmd in ("/start", "/help"):
        reply = (
            "🤖 *Relentless v1.0 Control Center*\n\n"
            "• `/digest` — Send top 5 unreviewed job matches\n"
            "• `/status` — Check database & label counts\n"
            "• `/eval` — View live Precision@K & Brier calibration report\n"
            "• `/reset_labels` — Soft Reset: clear button clicks & reset Eval Harness (`n=0`)\n"
            "• `/reset_triage` — Medium Reset: clear Jev scores + labels to re-score jobs\n"
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
    elif cmd == "/eval":
        reply = run_evaluation()
    elif cmd in ("/reset_labels", "/reset_triage", "/wipe_all"):
        mode_map = {"/reset_labels": "labels", "/reset_triage": "triage", "/wipe_all": "all"}
        mode = mode_map[cmd]
        raw_token, _expires = create_pending_action(
            "DESTRUCTIVE", {"mode": mode, "chat_id": int(chat_id) if chat_id else None}, ttl_seconds=600
        )
        confirm_text = (
            f"⚠️ Confirm destructive action: `{cmd}` (mode={mode})?\\n"
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
                cur.execute("SELECT job_id, status, router_path, started_at FROM apply_events ORDER BY started_at DESC LIMIT 10;")
                rows = cur.fetchall()
        lines = ["📋 Last apply events:"]
        for r in rows:
            lines.append(f"• Job {r['job_id']}: {r['status']} ({r['router_path']})")
        reply = "\\n".join(lines) if rows else "No apply events yet."
    else:
        return

    await client.post(
        f"{TG_BASE}/sendMessage",
        json={"chat_id": CHAT_ID, "text": reply, "parse_mode": "Markdown"},
    )

async def listen_for_button_clicks():
    if not CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID is required to run the Telegram listener")
    print("🎧 Listening for Telegram button taps & /commands (/help, /status, /eval, /reset_labels)...")
    offset = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                resp = await client.get(f"{TG_BASE}/getUpdates", params={"offset": offset, "timeout": 25})
                data = resp.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1

                    # Authorization: reject any update not from the configured chat.
                    if not is_authorized(upd):
                        # answer callback if present, generically
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
                    chat_id = update_chat_id(upd)

                    # 1. Check for text slash commands
                    text_in = msg_obj.get("text", "") if isinstance(msg_obj, dict) else ""
                    if isinstance(text_in, str) and text_in.startswith("/"):
                        await handle_slash_command(client, text_in, chat_id=chat_id)
                        continue
                    # Handle edit-instruction messages: "#123 <note>"
                    if isinstance(text_in, str) and text_in.strip().startswith("#"):
                        m = _re.match(r"^#(\d+)\s*(.*)$", text_in.strip(), _re.DOTALL)
                        if m:
                            jid = int(m.group(1))
                            note = m.group(2).strip()
                            with get_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("SELECT id, company, title, jd_text, jev_persona, apply_url FROM jobs WHERE id = %s;", (jid,))
                                    jr = cur.fetchone()
                            if jr:
                                await client.post(
                                    f"{TG_BASE}/sendMessage",
                                    json={
                                        "chat_id": chat_id or CHAT_ID,
                                        "text": f"✅ Job #{jid}: received modification instructions. I'll get back to you with an updated PDF.",
                                    },
                                )

                                async def _edit_task(jobr, n):
                                    try:
                                        thread_cfg = {"configurable": {"thread_id": f"job_{jobr['id']}"}}
                                        final_state = await RELENTLESS_GRAPH.ainvoke(
                                            {
                                                "job_id": jobr["id"],
                                                "company": jobr["company"],
                                                "title": jobr["title"],
                                                "jd_text": jobr["jd_text"] or "",
                                                "jev_persona": jobr["jev_persona"] or "ai_agent_engineer",
                                                "human_decision": "APPLIED",
                                                "custom_edit_note": n,
                                                "tailored_diff": None,
                                                "pdf_path": None,
                                            },
                                            config=thread_cfg,
                                        )
                                        pdf_path = final_state["pdf_path"]
                                        caption = f"Job #{jobr['id']}: {final_state['tailored_diff']}\n\n🔗 Apply Here: {jobr['apply_url']}"
                                        async with httpx.AsyncClient(timeout=60.0) as c:
                                            with open(pdf_path, "rb") as pdf_file:
                                                await c.post(
                                                    f"{TG_BASE}/sendDocument",
                                                    data={"chat_id": CHAT_ID, "caption": caption[:1000]},
                                                    files={"document": (os.path.basename(pdf_path), pdf_file, "application/pdf")},
                                                )
                                    except Exception as e:
                                        async with httpx.AsyncClient(timeout=30.0) as c:
                                            await c.post(
                                                f"{TG_BASE}/sendMessage",
                                                json={"chat_id": CHAT_ID, "text": f"⚠️ Job #{jobr['id']}: edit failed: {e}"},
                                            )

                                asyncio.create_task(_edit_task(jr, note))
                                continue

                    # 2. Check for inline button taps
                    cb = upd.get("callback_query")
                    if not cb:
                        continue

                    cb_id = cb["id"]
                    payload = cb.get("data", "")

                    # Handle confirmation/cancel callbacks
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
                        if payload_data.get("chat_id") is not None and int(payload_data["chat_id"]) != int(chat_id):
                            await client.post(
                                f"{TG_BASE}/answerCallbackQuery",
                                json={"callback_query_id": cb_id, "text": "Foreign token"},
                            )
                            continue
                        mode = payload_data.get("mode", "labels")
                        mode_to_reset = {"labels": "labels", "triage": "triage", "all": "all"}[mode]
                        msg = reset_database(mode_to_reset)
                        await client.post(
                            f"{TG_BASE}/sendMessage",
                            json={"chat_id": chat_id or CHAT_ID, "text": msg},
                        )
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb_id, "text": "Done"},
                        )
                        continue

                    # Handle prepare callback (2 parts)
                    if payload.startswith("prepare:"):
                        try:
                            job_id = int(payload.split(":", 1)[1])
                        except ValueError:
                            await client.post(f"{TG_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Bad id"})
                            continue
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT id, company, title, apply_url, jev_fit_prob, jev_fit_tier, jev_persona, tailored_pdf_path, tailored_pdf_sha256 FROM jobs WHERE id = %s;", (job_id,))
                                job_row = cur.fetchone()
                        if job_row:
                            job_dict = dict(job_row)
                            res = await prepare_application(job_dict)
                            feasible = res.get("feasible", False)
                            status = res.get("status")
                            # send review card
                            msg = (
                                f"📋 Job #{job_row['id']}: Prepared for {job_row['company']} — {job_row['title']}\\n"
                                f"Status: {status}\\n"
                                f"Feasible: {feasible}\\n"
                                f"URL: {job_row['apply_url']}"
                            )
                            buttons = []
                            if feasible and status in ("PREPARED", "DRY_RUN"):
                                # create submit confirmation token
                                raw_token, _ = create_apply_confirmation(
                                    job_id, kind="SUBMIT",
                                    payload={"pdf_sha256": job_row.get("tailored_pdf_sha256"), "apply_url": job_row["apply_url"]},
                                    ttl_seconds=600, created_by=str(chat_id),
                                )
                                buttons.append([{"text": "✅ Submit application", "callback_data": f"submit:{job_id}:{raw_token}"}])
                            buttons.append([{"text": "✏️ Edit / re-tailor", "callback_data": f"act:APPLIED:{job_id}"}])
                            buttons.append([{"text": "❌ Cancel", "callback_data": f"cancel_prepare:{job_id}"}])
                            await client.post(
                                f"{TG_BASE}/sendMessage",
                                json={"chat_id": chat_id or CHAT_ID, "text": msg, "reply_markup": {"inline_keyboard": buttons}},
                            )
                        await client.post(f"{TG_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Prepared"})
                        continue

                    # Handle submit callback (3 parts: submit:job_id:token)
                    if payload.startswith("submit:"):
                        parts = payload.split(":", 2)
                        if len(parts) != 3:
                            await client.post(f"{TG_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Bad payload"})
                            continue
                        job_id = int(parts[1])
                        token = parts[2]
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT id, company, title, apply_url, tailored_pdf_path, tailored_pdf_sha256 FROM jobs WHERE id = %s;", (job_id,))
                                job_row = cur.fetchone()
                        if job_row:
                            job_dict = dict(job_row)
                            res = await submit_application(job_dict, token)
                            await client.post(
                                f"{TG_BASE}/sendMessage",
                                json={"chat_id": chat_id or CHAT_ID, "text": f"Submit result for {job_row['company']}: {res.get('status')} {res.get('error') or ''}"},
                            )
                        await client.post(f"{TG_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Done"})
                        continue

                    parts = payload.split(":")
                    if len(parts) != 3:
                        continue

                    kind, label, job_id_str = parts[0], parts[1], parts[2]
                    job_id = int(job_id_str)

                    if kind == "apply":
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("SELECT id, company, title, apply_url, jev_fit_prob, jev_fit_tier, jev_persona FROM jobs WHERE id = %s;", (job_id,))
                                job_row = cur.fetchone()
                        if job_row:
                            job_dict = dict(job_row)
                            job_dict["pdf_path"] = None
                            res = await attempt_apply(job_dict, mode="CONFIRM")
                            await client.post(
                                f"{TG_BASE}/sendMessage",
                                json={"chat_id": CHAT_ID, "text": f"Apply result for {job_row['company']}: {res.get('status')} {res.get('error') or ''}"},
                            )
                        await client.post(f"{TG_BASE}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": "Apply triggered"})
                        continue

                    with get_connection() as conn:
                        with conn.cursor() as cur:
                            if kind == "act":
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET human_action = %s, human_action_at = NOW()
                                    WHERE id = %s
                                    RETURNING id, company, title, jd_text, jev_persona, apply_url;
                                    """,
                                    (label, job_id),
                                )
                            else:
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET outcome_status = %s
                                    WHERE id = %s
                                    RETURNING id, company, title, jd_text, jev_persona, apply_url;
                                    """,
                                    (label, job_id),
                                )
                            job_row = cur.fetchone()
                        conn.commit()

                    await client.post(
                        f"{TG_BASE}/answerCallbackQuery",
                        json={"callback_query_id": cb_id, "text": f"Logged: {label}!"},
                    )
                    print(f"✅ Ground-Truth Logged -> Job #{job_id}: {label}")

                    if kind == "act" and label == "APPLIED" and job_row:
                        print(f"⚙️ Running LangGraph Surgical Tailor + Typst PDF for [{job_row['company']}]...")
                        await client.post(
                            f"{TG_BASE}/sendMessage",
                            json={
                                "chat_id": CHAT_ID,
                                "text": f"⏳ Job #{job_row['id']}: tailoring resume & compiling PDF for *{job_row['company']}*... I'll send the result when done.",
                                "parse_mode": "Markdown",
                            },
                        )

                        async def _tailor_task(jr, note=""):
                            try:
                                thread_cfg = {"configurable": {"thread_id": f"job_{jr['id']}"}}
                                final_state = await RELENTLESS_GRAPH.ainvoke(
                                    {
                                        "job_id": jr["id"],
                                        "company": jr["company"],
                                        "title": jr["title"],
                                        "jd_text": jr["jd_text"] or "",
                                        "jev_persona": jr["jev_persona"] or "ai_agent_engineer",
                                        "human_decision": "APPLIED",
                                        "custom_edit_note": note,
                                        "tailored_diff": None,
                                        "pdf_path": None,
                                    },
                                    config=thread_cfg,
                                )
                                pdf_path = final_state["pdf_path"]
                                caption = f"Job #{jr['id']}: {final_state['tailored_diff']}\n\n🔗 Apply Here: {jr['apply_url']}"
                                async with httpx.AsyncClient(timeout=60.0) as c:
                                    with open(pdf_path, "rb") as pdf_file:
                                        await c.post(
                                            f"{TG_BASE}/sendDocument",
                                            data={"chat_id": CHAT_ID, "caption": caption[:1000]},
                                            files={"document": (os.path.basename(pdf_path), pdf_file, "application/pdf")},
                                        )
                                print(f"📄 Delivered tailored PDF to Telegram: {pdf_path}")
                            except Exception as e:
                                async with httpx.AsyncClient(timeout=30.0) as c:
                                    await c.post(
                                        f"{TG_BASE}/sendMessage",
                                        json={"chat_id": CHAT_ID, "text": f"⚠️ Job #{jr['id']}: tailoring failed: {e}"},
                                    )

                        asyncio.create_task(_tailor_task(job_row))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.getLogger(__name__).error("Loop error: %s", e)
                alert(f"⚠️ Loop error: {e}")
                await asyncio.sleep(3)

async def main():
    await send_pending_digests(limit=3)
    await listen_for_button_clicks()

if __name__ == "__main__":
    asyncio.run(main())