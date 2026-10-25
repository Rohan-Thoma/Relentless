import asyncio
import logging
import os

from src.apply.runner import prepare_apply_cycle
from src.config import load_config
from src.db import init_db
from src.hitl.telegram_bot import listen_for_button_clicks, send_pending_digests
from src.ingestors.ats_poller import poll_all_ats, poll_all_scraping
from src.knowledge.distiller import run_distill
from src.knowledge.store import hydrate
from src.notify import alert
from src.triage.jev_judge import run_triage

logger = logging.getLogger(__name__)

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "1.0"))  # Faster response (was 4.0)


async def _process_digest_and_triage(context_label: str):
    """Processes digests, runs triage, and triggers auto-apply preparation."""
    # STREAMING: Send digest immediately after finding jobs (before triage completes)
    await send_pending_digests(limit=5)
    print(f"📨 Digest sent (new jobs found from {context_label})", flush=True)
    await run_triage(batch_limit=100)
    # STREAMING: Send updated scores after triage
    await send_pending_digests(limit=5)
    print(f"📨 Digest updated (triage scores for {context_label})", flush=True)
    try:
        await prepare_apply_cycle()
        # STREAMING: Send apply status updates
        await send_pending_digests(limit=5)
        print(f"📨 Digest updated (apply events for {context_label})", flush=True)
    except Exception as e:
        logger.error("Auto apply cycle error: %s", e)
        alert(f"⚠️ Auto apply cycle error: {e}")


async def scheduled_pipeline_worker(
    scrape_only: bool = False,
    skip_ats: bool = False,
    ats_only: bool = False,
    sources: list[str] | None = None,
    once: bool = False,
):
    """Unified Pipeline Worker.

    1. Phase 1 (First Preference): High-speed scraping across JobSpy (Naukri, LinkedIn, Indeed, Google)
       and Gmail alert inbox. Fresh jobs are immediately triaged and digested for the user.
    2. Phase 2: Leisurely ATS Crawling across Greenhouse, Lever, Ashby, Workday, etc.
       (Can be completely bypassed via --scrape-only / --fast / --skip-ats).
    """
    cfg = load_config()
    ingest_cfg = getattr(cfg, "ingestion", None)

    env_scrape_only = os.getenv("RELENTLESS_SCRAPE_ONLY", "").lower() in ("1", "true", "yes")
    env_skip_ats = os.getenv("RELENTLESS_SKIP_ATS", "").lower() in ("1", "true", "yes")

    cfg_scrape_only = getattr(ingest_cfg, "scrape_only", False) if ingest_cfg else False
    cfg_enable_ats = getattr(ingest_cfg, "enable_ats", True) if ingest_cfg else True

    do_scrape_only = scrape_only or env_scrape_only or cfg_scrape_only
    do_skip_ats = skip_ats or env_skip_ats or not cfg_enable_ats or do_scrape_only
    do_ats_only = ats_only

    while True:
        try:
            print("\n⏰ [Scheduler] Starting scheduled Ingestion + Jev Triage cycle...", flush=True)

            # --- Phase 1: High-Speed Scraping (First Preference) ---
            if not do_ats_only:
                print("\n⚡ [Pipeline Phase 1: High-Speed Scraping] Polling JobSpy multi-board & Gmail alerts...", flush=True)
                scrape_new = await poll_all_scraping(sources=sources)
                print(f"⚡ [Pipeline Phase 1] Scraped {scrape_new} matching jobs.", flush=True)
                if scrape_new > 0:
                    await _process_digest_and_triage("Scraped Jobs")
                else:
                    # Run a triage pass in case there are pending jobs from backlog
                    await send_pending_digests(limit=5)
                    await run_triage(batch_limit=50)

            # --- Phase 2: Leisurely ATS Crawling (Secondary) ---
            if not do_skip_ats and not do_scrape_only:
                print("\n🐢 [Pipeline Phase 2: Leisurely ATS Crawling] Scanning Greenhouse, Lever, Ashby, Workday...", flush=True)
                ats_new = await poll_all_ats()
                print(f"🐢 [Pipeline Phase 2] Discovered {ats_new} jobs from ATS boards.", flush=True)
                if ats_new > 0:
                    await _process_digest_and_triage("ATS Boards")
            else:
                if do_scrape_only or do_skip_ats:
                    print("⏩ [Pipeline Phase 2] ATS Polling skipped (Scrape-only / Fast mode active).", flush=True)

            # Post-cycle knowledge distillation
            try:
                d_res = run_distill()
                print(f"🧠 [OKF Brain] Distillation complete: {d_res}", flush=True)
            except Exception as e:
                logger.error("Distillation error: %s", e)

            print("✅ [Scheduler] Cycle complete.", flush=True)
            if once:
                print("🛑 Single cycle execution complete (--once active). Exiting.")
                break

            print(f"Sleeping for {POLL_INTERVAL_HOURS} hours...")
        except Exception as e:
            logger.error("Scheduler error during cycle: %s", e)
            alert(f"⚠️ Scheduler error during cycle: {e}")
            if once:
                break
        await asyncio.sleep(POLL_INTERVAL_HOURS * 3600)


async def main(
    scrape_only: bool = False,
    skip_ats: bool = False,
    ats_only: bool = False,
    sources: list[str] | None = None,
    once: bool = False,
):
    init_db()
    hydrate()
    if once:
        await scheduled_pipeline_worker(
            scrape_only=scrape_only,
            skip_ats=skip_ats,
            ats_only=ats_only,
            sources=sources,
            once=True,
        )
    else:
        await asyncio.gather(
            scheduled_pipeline_worker(
                scrape_only=scrape_only,
                skip_ats=skip_ats,
                ats_only=ats_only,
                sources=sources,
                once=False,
            ),
            listen_for_button_clicks(),
        )


if __name__ == "__main__":
    asyncio.run(main())