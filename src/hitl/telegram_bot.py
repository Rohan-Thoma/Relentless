import os
import asyncio
import httpx
from dotenv import load_dotenv
from src.db import get_connection

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def build_inline_keyboard(job_id: int, apply_url: str, current_action: str | None = None) -> dict:
    """Builds the Telegram inline keyboard that captures ground-truth labels on every tap."""
    if current_action:
        return {
            "inline_keyboard": [
                [{"text": f"🏷️ Logged: {current_action} (Open Application ↗)", "url": apply_url}],
                [{"text": "⭐ Mark Recruiter Callback!", "callback_data": f"out:CALLBACK:{job_id}"}],
            ]
        }
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Apply / Tailor", "callback_data": f"act:APPLIED:{job_id}"},
                {"text": "🔗 View JD", "url": apply_url},
            ],
            [
                {"text": "👎 Skip: Bad Fit", "callback_data": f"act:SKIP_BAD_FIT:{job_id}"},
                {"text": "🚫 Skip: Ghost/Scam", "callback_data": f"act:SKIP_SCAM:{job_id}"},
            ],
        ]
    }

def format_job_card(job: dict) -> str:
    trust_pct = int((1.0 - (job["jev_scam_prob"] or 0.0)) * 100)
    fit_pct = int((job["jev_fit_prob"] or 0.0) * 100)
    base_pct = int((job["cosine_sim"] or 0.0) * 100)
    return (
        f"🎯 *{job['title']}*\n"
        f"🏢 *{job['company']}* ({job['source_ats'].upper()})\n"
        f"📍 {job['location']}\n\n"
        f"📊 *Jev Fit Prob:* `{fit_pct}%` (`{job['jev_fit_tier']}`)\n"
        f"🛡️ *Trust Score:* `{trust_pct}/100` | *Lexical Base:* `{base_pct}%`\n"
        f"🎭 *Persona:* `{job['jev_persona']}`\n\n"
        f"_Tap an action below to log ground-truth to Neon Postgres:_"
    )

async def send_pending_digest(limit: int = 10):
    """Pushes triaged jobs that haven't been reviewed yet to your Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first!")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM jobs
                WHERE triage_passed = TRUE AND human_action IS NULL
                ORDER BY jev_fit_prob DESC
                LIMIT %s;
                """,
                (limit,),
            )
            jobs = cur.fetchall()

    if not jobs:
        print("📭 No unreviewed passing jobs to send right now.")
        return

    async with httpx.AsyncClient(timeout=15.0) as client:
        for job in jobs:
            payload = {
                "chat_id": CHAT_ID,
                "text": format_job_card(job),
                "parse_mode": "Markdown",
                "reply_markup": build_inline_keyboard(job["id"], job["apply_url"]),
            }
            resp = await client.post(f"{TG_BASE}/sendMessage", json=payload)
            if resp.status_code == 200:
                print(f"📲 Sent Telegram card for [{job['company']}] {job['title']}")
            await asyncio.sleep(0.4)

async def listen_for_button_clicks():
    """Long-polls Telegram for inline button clicks and writes ground-truth labels to Neon Postgres."""
    print("📡 Listening for your Telegram button taps (Press Ctrl+C to stop)...")
    offset = 0
    async with httpx.AsyncClient(timeout=35.0) as client:
        while True:
            try:
                resp = await client.get(f"{TG_BASE}/getUpdates", params={"offset": offset, "timeout": 25})
                data = resp.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    cb = upd.get("callback_query")
                    if not cb:
                        continue

                    cb_data = cb.get("data", "")
                    parts = cb_data.split(":")
                    if len(parts) != 3:
                        continue

                    kind, label, job_id_str = parts
                    job_id = int(job_id_str)

                    with get_connection() as conn:
                        with conn.cursor() as cur:
                            if kind == "act":
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET human_action = %s, human_action_at = NOW()
                                    WHERE id = %s
                                    RETURNING apply_url, company, title;
                                    """,
                                    (label, job_id),
                                )
                            else:
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET outcome_status = %s
                                    WHERE id = %s
                                    RETURNING apply_url, company, title;
                                    """,
                                    (label, job_id),
                                )
                            row = cur.fetchone()
                        conn.commit()

                    if row:
                        print(f"✅ Ground-Truth Logged in Postgres: Job #{job_id} ({row['company']}) -> {label}")
                        # Acknowledge button tap on phone
                        await client.post(
                            f"{TG_BASE}/answerCallbackQuery",
                            json={"callback_query_id": cb["id"], "text": f"Logged: {label}!"},
                        )
                        # Update button UI on the card so you know it's already labeled
                        msg = cb.get("message", {})
                        await client.post(
                            f"{TG_BASE}/editMessageReplyMarkup",
                            json={
                                "chat_id": msg.get("chat", {}).get("id"),
                                "message_id": msg.get("message_id"),
                                "reply_markup": build_inline_keyboard(job_id, row["apply_url"], current_action=label),
                            },
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ Polling retry: {e}")
                await asyncio.sleep(3)

async def main():
    await send_pending_digest(limit=5)
    await listen_for_button_clicks()

if __name__ == "__main__":
    asyncio.run(main())