import os
import asyncio
from src.db import init_db
from src.ingestors.ats_poller import run_ingestion
from src.triage.jev_judge import run_triage
from src.hitl.telegram_bot import send_pending_digests, listen_for_button_clicks

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "4.0"))

async def scheduled_pipeline_worker():
    """Runs ATS Polling -> Jev System-1 Triage -> Telegram Digest every N hours."""
    while True:
        try:
            print("\n⏰ [Scheduler] Starting scheduled Ingestion + Jev Triage cycle...")
            await run_ingestion()
            await run_triage(batch_limit=30)
            await send_pending_digests(limit=5)
            print(f"✅ [Scheduler] Cycle complete. Sleeping for {POLL_INTERVAL_HOURS} hours...")
        except Exception as e:
            print(f"⚠️ [Scheduler] Error during cycle: {e}")
        await asyncio.sleep(POLL_INTERVAL_HOURS * 3600)

async def main():
    init_db()
    await asyncio.gather(
        scheduled_pipeline_worker(),
        listen_for_button_clicks(),
    )

if __name__ == "__main__":
    asyncio.run(main())