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
from src.logging_config import configure_logging
from src.notify import alert
from src.pipeline import TASK_REGISTRY, PipelineEngine
from src.readiness import collect_readiness, log_startup_readiness
from src.triage.jev_judge import run_triage

logger = logging.getLogger(__name__)

POLL_INTERVAL_HOURS = float(os.getenv("POLL_INTERVAL_HOURS", "1.0"))


async def _wait_until_profile_configured(worker_name: str) -> None:
    """Pauses background workers cleanly until Master Profile is configured via Web or CLI."""
    logged = False
    while True:
        try:
            rep = await asyncio.to_thread(collect_readiness, False)
            if rep.get("profile_configured"):
                return
        except Exception as e:
            logger.warning("[%s] Readiness check error: %s", worker_name, e)
        if not logged:
            logger.warning(
                "[%s] On standby — Master Profile is not configured yet. Set it in Web UI -> 06 // CONFIG & VAULT or run 'python manage.py setup'.",
                worker_name,
            )
            logged = True
        await asyncio.sleep(30.0)


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


def _resolve_active_producers(
    cfg,
    scrape_only: bool = False,
    skip_ats: bool = False,
    ats_only: bool = False,
    sources: list[str] | None = None,
) -> dict[str, bool]:
    """Single canonical resolver for which producers are active across BOTH daemon & --once modes."""
    ingest_cfg = cfg.ingestion
    env_scrape_only = os.getenv("RELENTLESS_SCRAPE_ONLY", "").strip().lower() in ("1", "true", "yes")
    env_skip_ats = os.getenv("RELENTLESS_SKIP_ATS", "").strip().lower() in ("1", "true", "yes")

    src_filter = {s.strip().lower() for s in sources} if sources else None

    if ats_only:
        return {
            "jobboards": False,
            "gmail": False,
            "ats": True,
            "workday": bool(ingest_cfg.enable_workday),
            "remote_feeds": bool(ingest_cfg.enable_remote_feeds),
        }

    do_scrape_only = bool(scrape_only or env_scrape_only or ingest_cfg.scrape_only)
    do_skip_ats = bool(skip_ats or env_skip_ats or do_scrape_only or not ingest_cfg.enable_ats)

    enable_jobboards = bool(ingest_cfg.enable_jobboards) and (
        src_filter is None or "jobboards" in src_filter
    )
    enable_gmail = bool(ingest_cfg.enable_gmail) and (
        src_filter is None or "gmail" in src_filter
    )
    enable_ats = (not do_skip_ats) and (src_filter is None or "ats" in src_filter)
    enable_workday = (
        enable_ats and bool(ingest_cfg.enable_workday)
    )
    enable_remote_feeds = (not do_scrape_only) and bool(ingest_cfg.enable_remote_feeds)

    return {
        "jobboards": enable_jobboards,
        "gmail": enable_gmail,
        "ats": enable_ats,
        "workday": enable_workday,
        "remote_feeds": enable_remote_feeds,
    }


async def scheduled_pipeline_worker(
    scrape_only: bool = False,
    skip_ats: bool = False,
    ats_only: bool = False,
    sources: list[str] | None = None,
    once: bool = False,
):
    """Unified Pipeline Worker for single-cycle execution (--once) or batch fallback."""
    cfg = load_config()
    active = _resolve_active_producers(
        cfg,
        scrape_only=scrape_only,
        skip_ats=skip_ats,
        ats_only=ats_only,
        sources=sources,
    )

    engine = PipelineEngine(triage_concurrency=cfg.ingestion.triage_concurrency)
    await engine.start()

    try:
        while True:
            print("\n⏰ [Scheduler] Starting Ingestion cycle...", flush=True)

            # Phase 1: High-Speed Scraping (JobSpy & Gmail)
            if active["jobboards"] or active["gmail"]:
                scrape_sources = []
                if active["jobboards"]:
                    scrape_sources.append("jobboards")
                if active["gmail"]:
                    scrape_sources.append("gmail")
                print(
                    f"\n⚡ [Pipeline Phase 1: High-Speed Scraping] Polling {', '.join(scrape_sources)}...",
                    flush=True,
                )
                scrape_new = await poll_all_scraping(sources=scrape_sources)
                print(f"⚡ [Pipeline Phase 1] Scraped {scrape_new} matching jobs.", flush=True)

            # Phase 2: Smart Self-Pruning ATS Crawling
            if active["ats"]:
                print(
                    "\n🐢 [Pipeline Phase 2: Direct ATS Boards] Scanning Greenhouse, Lever, Ashby, SmartRecruiters...",
                    flush=True,
                )
                ats_new = await poll_all_ats()
                print(f"🐢 [Pipeline Phase 2] Discovered {ats_new} jobs from ATS boards.", flush=True)
            else:
                print("⏩ [Pipeline Phase 2] ATS Polling skipped (disabled in config or flags).", flush=True)

            # Drain streaming triage and notify queues
            await engine.drain()

            # Ensure any lingering triage/digest items are flushed
            await _process_digest_and_triage("Ingested Jobs")

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


async def _gmail_producer_loop(interval_minutes: int):
    interval_sec = max(60.0, float(interval_minutes) * 60.0)
    while True:
        await _wait_until_profile_configured("Gmail Scanner")
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


async def _jobboards_producer_loop(interval_minutes: int):
    interval_sec = max(60.0, float(interval_minutes) * 60.0)
    while True:
        await _wait_until_profile_configured("JobSpy Boards")
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


async def _ats_producer_loop(interval_minutes: int, platforms: list[str] | None = None, task_label: str = "ATS Crawler"):
    interval_sec = max(300.0, float(interval_minutes) * 60.0)
    while True:
        await _wait_until_profile_configured(task_label)
        try:
            TASK_REGISTRY.set_system_task(task_label)
            await poll_all_ats(platforms=platforms)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("%s error: %s", task_label, e)
        finally:
            TASK_REGISTRY.clear_system_task(task_label)
        await asyncio.sleep(interval_sec)


async def _distill_loop():
    interval_sec = 1800.0
    while True:
        await _wait_until_profile_configured("OKF Distiller")
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
    configure_logging(capture_stdio=True)
    init_db()
    hydrate()
    try:
        from src.ingestors.dedup_cache import CACHE

        CACHE.hydrate_from_db()
    except Exception as e:
        logger.warning("Dedup cache hydration skipped: %s", e)

    report = log_startup_readiness()
    if not report.get("ready"):
        missing = ", ".join(report.get("missing_required") or [])
        logger.error("Relentless startup detected incomplete configuration: %s", missing)
        alert(
            f"🚨 Relentless started with missing setup ({missing}). "
            "Open the Web UI (06 // CONFIG & VAULT or 07 // LOGS & DIAG) or run /logs in Telegram."
        )
        if once:
            raise RuntimeError(f"Cannot execute pipeline (--once): missing required setup: {missing}")

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
        ingest_cfg = cfg.ingestion
        scanner_cfg = cfg.gmail_scanner
        active = _resolve_active_producers(
            cfg,
            scrape_only=scrape_only,
            skip_ats=skip_ats,
            ats_only=ats_only,
            sources=sources,
        )

        engine = PipelineEngine(triage_concurrency=ingest_cfg.triage_concurrency)

        coros = [engine.run(), _distill_loop()]

        if os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip():
            coros.insert(0, listen_for_button_clicks())
        else:
            logger.warning(
                "Telegram bot listener disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable Telegram alerts & /logs."
            )

        if active["gmail"]:
            gm_interval = scanner_cfg.poll_interval_minutes or ingest_cfg.gmail_interval_minutes
            coros.append(_gmail_producer_loop(gm_interval))

        if active["jobboards"]:
            jb_interval = int(
                os.getenv("JOBBOARDS_INTERVAL_MINUTES")
                or (POLL_INTERVAL_HOURS * 60 if os.getenv("POLL_INTERVAL_HOURS") else ingest_cfg.jobboards_interval_minutes)
            )
            coros.append(_jobboards_producer_loop(jb_interval))

        if active["ats"]:
            fast_plats = ["greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"]
            if active["remote_feeds"]:
                fast_plats.extend(["remotive", "arbeitnow"])
            coros.append(
                _ats_producer_loop(
                    ingest_cfg.ats_interval_minutes,
                    platforms=fast_plats,
                    task_label="ATS Crawler",
                )
            )
        elif active["remote_feeds"]:
            coros.append(
                _ats_producer_loop(
                    ingest_cfg.ats_interval_minutes,
                    platforms=["remotive", "arbeitnow"],
                    task_label="Remote Feeds",
                )
            )

        if active["workday"]:
            coros.append(
                _ats_producer_loop(
                    ingest_cfg.workday_interval_minutes,
                    platforms=["workday_cxs"],
                    task_label="Workday Crawler",
                )
            )

        if os.getenv("ENABLE_WEB_SERVER", "1").lower() not in ("0", "false", "no"):
            from src.web.api import run_web_server

            coros.append(run_web_server())

        active_names = [k for k, v in active.items() if v]
        logger.info(
            "Starting Relentless v4 Unified Pipeline (Producers: %s%s)...",
            ", ".join(active_names) or "none",
            " + Telegram Bot"
            if (os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())
            else "",
        )
        await asyncio.gather(*coros)


if __name__ == "__main__":
    asyncio.run(main())
