import asyncio
import logging
import os

from src.db import init_db
from src.hitl.telegram_bot import listen_for_button_clicks, send_pending_digests
from src.ingestors.ats_poller import run_ingestion
from src.notify import alert
from src.triage.jev_judge import run_triage

logger = logging.getLogger(__name__)

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "4.0"))

async def scheduled_pipeline_worker():
    """Runs ATS Polling -> Jev System-1 Triage -> Telegram Digest every N hours."""
    while True:
        try:
            print("\n⏰ [Scheduler] Starting scheduled Ingestion + Jev Triage cycle...")
            await run_ingestion()
            await run_triage(batch_limit=30)
            await send_pending_digests(limit=5)
            try:
                from src.apply.runner import auto_apply_cycle
                await auto_apply_cycle()
            except Exception as e:
                logger.error("Auto apply cycle error: %s", e)
                alert(f"⚠️ Auto apply cycle error: {e}")
            print(f"✅ [Scheduler] Cycle complete. Sleeping for {POLL_INTERVAL_HOURS} hours...")
        except Exception as e:
            logger.error("Scheduler error during cycle: %s", e)
            alert(f"⚠️ Scheduler error during cycle: {e}")
        await asyncio.sleep(POLL_INTERVAL_HOURS * 3600)

async def main():
    init_db()
    await asyncio.gather(
        scheduled_pipeline_worker(),
        listen_for_button_clicks(),
    )

if __name__ == "__main__":
    asyncio.run(main())