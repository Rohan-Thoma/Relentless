import asyncio
import logging
import os

from src.apply.runner import prepare_apply_cycle
from src.config import load_config
from src.db import init_db
from src.hitl.telegram_bot import listen_for_button_clicks, send_pending_digests
from src.ingestors.ats_poller import poll_all_ats, poll_all_scraping
from src.ingestors.gmail_ingestor import poll_gmail_alerts
from src.ingestors.jobboards_poller import poll_jobboards
from src.knowledge.distiller import run_distill
from src.knowledge.store import hydrate
from src.notify import alert
from src.pipeline import PipelineEngine, TASK_REGISTRY
from src.triage.jev_judge import run_triage

logger = logging.getLogger(__name__)

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "1.0"))


async def _process_digest_and_triage(context_label: str):
    """Processes digests, runs triage, and triggers auto-apply preparation."""
    await send_pending_digests(limit=5)
    print(f"📨 Digest sent (new jobs found from {context_label})", flush=True)
    await run_triage(batch_limit=100)
    await send_pending_digests(limit=5)
    print(f"📨 Digest updated (triage scores for {context_label})", flush=True)
    try:
        await prepare_apply_cycle()
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
    """Unified Pipeline Worker for single-cycle execution (--once) or batch fallback."""
    cfg = load_config()
    ingest_cfg = getattr(cfg, "ingestion", None)

    env_scrape_only = os.getenv("RELENTLESS_SCRAPE_ONLY", "").lower() in ("1", "true", "yes")
    env_skip_ats = os.getenv("RELENTLESS_SKIP_ATS", "").lower() in ("1", "true", "yes")

    cfg_scrape_only = getattr(ingest_cfg, "scrape_only", False) if ingest_cfg else False
    cfg_enable_ats = getattr(ingest_cfg, "enable_ats", True) if ingest_cfg else True

    do_scrape_only = scrape_only or env_scrape_only or cfg_scrape_only
    do_skip_ats = skip_ats or env_skip_ats or not cfg_enable_ats or do_scrape_only
    do_ats_only = ats_only

    engine = PipelineEngine(
        triage_concurrency=getattr(ingest_cfg, "triage_concurrency", 3) if ingest_cfg else 3
    )
    await engine.start()

    try:
        while True:
            print("\n⏰ [Scheduler] Starting Ingestion cycle...", flush=True)

            # Phase 1: High-Speed Scraping
            if not do_ats_only:
                print("\n⚡ [Pipeline Phase 1: High-Speed Scraping] Polling JobSpy & Gmail alerts...", flush=True)
                scrape_new = await poll_all_scraping(sources=sources)
                print(f"⚡ [Pipeline Phase 1] Scraped {scrape_new} matching jobs.", flush=True)

            # Phase 2: Leisurely ATS Crawling
            if not do_skip_ats and not do_scrape_only:
                print("\n🐢 [Pipeline Phase 2: Leisurely ATS Crawling] Scanning Greenhouse, Lever, Ashby, Workday...", flush=True)
                ats_new = await poll_all_ats()
                print(f"🐢 [Pipeline Phase 2] Discovered {ats_new} jobs from ATS boards.", flush=True)
            else:
                if do_scrape_only or do_skip_ats:
                    print("⏩ [Pipeline Phase 2] ATS Polling skipped (Scrape-only / Fast mode active).", flush=True)

            # Drain streaming triage and notify queues
            await engine.drain()

            # Ensure any lingering triage/digest items are flushed
            await _process_digest_and_triage("Scraped Jobs")

            # Distillation
            try:
                d_res = await asyncio.to_thread(run_distill)
                print(f"🧠 [OKF Brain] Distillation complete: {d_res}", flush=True)
            except Exception as e:
                logger.error("Distillation error: %s", e)

            print("✅ [Scheduler] Cycle complete.", flush=True)
            if once:
                print("🛑 Single cycle execution complete (--once active). Exiting.")
                break

            await asyncio.sleep(POLL_INTERVAL_HOURS * 3600)
    finally:
        await engine.stop()


async def _gmail_producer_loop(scanner_cfg):
    interval_sec = float(getattr(scanner_cfg, "poll_interval_minutes", 20)) * 60.0
    while True:
        try:
            TASK_REGISTRY.set_system_task("Gmail Scanner")
            await poll_gmail_alerts()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Gmail producer error: %s", e)
        finally:
            TASK_REGISTRY.clear_system_task("Gmail Scanner")
        await asyncio.sleep(interval_sec)


async def _jobboards_producer_loop(poll_interval_hours: float):
    interval_sec = max(60.0, poll_interval_hours * 3600.0)
    while True:
        try:
            TASK_REGISTRY.set_system_task("JobSpy Boards")
            await poll_jobboards()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("JobSpy producer error: %s", e)
        finally:
            TASK_REGISTRY.clear_system_task("JobSpy Boards")
        await asyncio.sleep(interval_sec)


async def _ats_producer_loop():
    interval_sec = 4.0 * 3600.0
    while True:
        try:
            TASK_REGISTRY.set_system_task("ATS Crawler")
            await poll_all_ats()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("ATS crawler error: %s", e)
        finally:
            TASK_REGISTRY.clear_system_task("ATS Crawler")
        await asyncio.sleep(interval_sec)


async def _distill_loop():
    interval_sec = 1800.0
    while True:
        try:
            await asyncio.to_thread(run_distill)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Distillation loop error: %s", e)
        await asyncio.sleep(interval_sec)


async def main(
    scrape_only: bool = False,
    skip_ats: bool = False,
    ats_only: bool = False,
    sources: list[str] | None = None,
    once: bool = False,
):
    init_db()
    hydrate()
    try:
        from src.ingestors.dedup_cache import CACHE

        CACHE.hydrate_from_db()
    except Exception:
        pass

    if once:
        await scheduled_pipeline_worker(
            scrape_only=scrape_only,
            skip_ats=skip_ats,
            ats_only=ats_only,
            sources=sources,
            once=True,
        )
    else:
        cfg = load_config()
        ingest_cfg = getattr(cfg, "ingestion", None)
        scanner_cfg = getattr(cfg, "gmail_scanner", None)

        engine = PipelineEngine(
            triage_concurrency=getattr(ingest_cfg, "triage_concurrency", 3) if ingest_cfg else 3
        )

        enable_ats = getattr(ingest_cfg, "enable_ats", True) if ingest_cfg else True
        if skip_ats or scrape_only or os.getenv("RELENTLESS_SKIP_ATS") in ("1", "true"):
            enable_ats = False

        coros = [
            listen_for_button_clicks(),
            engine.run(),
            _gmail_producer_loop(scanner_cfg),
            _jobboards_producer_loop(POLL_INTERVAL_HOURS),
            _distill_loop(),
        ]
        if enable_ats and not ats_only:
            coros.append(_ats_producer_loop())

        print("🚀 Starting Relentless Unified Pipeline (Telegram Bot + Concurrent Streaming Producers)...", flush=True)
        await asyncio.gather(*coros)


if __name__ == "__main__":
    asyncio.run(main())
