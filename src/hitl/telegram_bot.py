import os
import asyncio
import httpx
from dotenv import load_dotenv
from src.db import get_connection
from src.orchestrator.graph import RELENTLESS_GRAPH

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
        ]
    }

async def send_pending_digests(limit: int = 5):
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_ats, company, title, location, apply_url,
                       jev_fit_prob, jev_fit_tier, jev_scam_prob, jev_persona, cosine_sim
                FROM jobs
                WHERE jev_fit_prob IS NOT NULL
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

async def listen_for_button_clicks():
    print("🎧 Listening for Telegram button taps (Tap '✅ Apply / Tailor PDF' on your phone!)...")
    offset = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                resp = await client.get(f"{TG_BASE}/getUpdates", params={"offset": offset, "timeout": 25})
                data = resp.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    cb = upd.get("callback_query")
                    if not cb:
                        continue

                    cb_id = cb["id"]
                    payload = cb.get("data", "")
                    parts = payload.split(":")
                    if len(parts) != 3:
                        continue

                    kind, label, job_id_str = parts[0], parts[1], parts[2]
                    job_id = int(job_id_str)

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

                    # If user tapped APPLY, invoke LangGraph Surgical Tailor + Typst PDF compiler!
                    if kind == "act" and label == "APPLIED" and job_row:
                        print(f"⚙️ Running LangGraph Surgical Tailor + Typst PDF for [{job_row['company']}]...")
                        await client.post(
                            f"{TG_BASE}/sendMessage",
                            json={
                                "chat_id": CHAT_ID,
                                "text": f"⏳ Tailoring resume & compiling Typst PDF for *{job_row['company']}*...",
                                "parse_mode": "Markdown",
                            },
                        )
                        thread_cfg = {"configurable": {"thread_id": f"job_{job_id}"}}
                        final_state = await RELENTLESS_GRAPH.ainvoke(
                            {
                                "job_id": job_row["id"],
                                "company": job_row["company"],
                                "title": job_row["title"],
                                "jd_text": job_row["jd_text"] or "",
                                "jev_persona": job_row["jev_persona"] or "ai_agent_engineer",
                                "human_decision": "APPLIED",
                                "custom_edit_note": "",
                                "tailored_diff": None,
                                "pdf_path": None,
                            },
                            config=thread_cfg,
                        )

                        pdf_path = final_state["pdf_path"]
                        caption = f"{final_state['tailored_diff']}\n\n🔗 Apply Here: {job_row['apply_url']}"
                        with open(pdf_path, "rb") as pdf_file:
                            await client.post(
                                f"{TG_BASE}/sendDocument",
                                data={"chat_id": CHAT_ID, "caption": caption[:1000]},
                                files={"document": (os.path.basename(pdf_path), pdf_file, "application/pdf")},
                            )
                        print(f"📄 Delivered tailored PDF to Telegram: {pdf_path}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ Loop error: {e}")
                await asyncio.sleep(3)

async def main():
    await send_pending_digests(limit=3)
    await listen_for_button_clicks()

if __name__ == "__main__":
    asyncio.run(main())