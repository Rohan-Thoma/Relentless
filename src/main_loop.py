import asyncio
import logging
import os

from src.db import init_db
from src.apply.runner import prepare_apply_cycle
from src.knowledge.store import hydrate
from src.hitl.telegram_bot import listen_for_button_clicks, send_pending_digests
from src.ingestors.ats_poller import run_ingestion
from src.notify import alert
from src.triage.jev_judge import run_triage

logger = logging.getLogger(__name__)

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "1.0"))  # Faster response (was 4.0)

async def scheduled_pipeline_worker():
    """Runs ATS Polling -> Jev System-1 Triage -> Telegram Digest every N hours."""
    while True:
        try:
            print("\n⏰ [Scheduler] Starting scheduled Ingestion + Jev Triage cycle...")
            await run_ingestion()
            # STREAMING: Send digest immediately after finding jobs (before triage completes)
            await send_pending_digests(limit=5)
            print("📨 Digest sent (new jobs found)")
            await run_triage(batch_limit=30)
            # STREAMING: Send updated scores after triage
            await send_pending_digests(limit=5)
            print("📨 Digest updated (triage scores)")
            try:
                await prepare_apply_cycle()
                # STREAMING: Send apply status updates
                await send_pending_digests(limit=5)
                print("📨 Digest updated (apply events)")
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
    hydrate()
    await asyncio.gather(
        scheduled_pipeline_worker(),
        listen_for_button_clicks(),
    )

if __name__ == "__main__":
    asyncio.run(main())