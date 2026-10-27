import asyncio
import logging
import os
import threading

import httpx

from src.db import get_connection
from src.ingestors.common import set_job_inserted_hook
from src.triage.jev_judge import score_single_job

logger = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._job_tasks: dict[int, str] = {}
        self._system_tasks: set[str] = set()

    def set_job_stage(self, job_id: int, stage_desc: str) -> None:
        with self._lock:
            self._job_tasks[job_id] = stage_desc

    def clear_job_stage(self, job_id: int) -> None:
        with self._lock:
            self._job_tasks.pop(job_id, None)

    def get_job_stage(self, job_id: int) -> str | None:
        with self._lock:
            return self._job_tasks.get(job_id)

    def set_system_task(self, task_name: str) -> None:
        with self._lock:
            self._system_tasks.add(task_name)

    def clear_system_task(self, task_name: str) -> None:
        with self._lock:
            self._system_tasks.discard(task_name)

    def get_all_status(self) -> dict:
        with self._lock:
            return {
                "active_jobs": dict(self._job_tasks),
                "system_tasks": list(self._system_tasks),
            }


TASK_REGISTRY = TaskRegistry()


class PipelineEngine:
    def __init__(self, triage_concurrency: int = 3):
        self.triage_concurrency = triage_concurrency
        self.triage_queue: asyncio.Queue[dict] = asyncio.Queue()
        self.notify_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._queued_triage_ids: set[int] = set()
        self._queued_notify_ids: set[int] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running: bool = False
        self._tasks: list[asyncio.Task] = []

    def enqueue_discovered_job(self, job_dict: dict) -> None:
        job_id = job_dict.get("id")
        if job_id and job_id in self._queued_triage_ids:
            return
        if job_id:
            self._queued_triage_ids.add(job_id)

        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if self._loop and self._loop.is_running() and curr_loop is not self._loop:
            self._loop.call_soon_threadsafe(self.triage_queue.put_nowait, job_dict)
        else:
            self.triage_queue.put_nowait(job_dict)

    def enqueue_notify_job(self, job_dict: dict) -> None:
        job_id = job_dict.get("id")
        if job_id and job_id in self._queued_notify_ids:
            return
        if job_id:
            self._queued_notify_ids.add(job_id)

        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if self._loop and self._loop.is_running() and curr_loop is not self._loop:
            self._loop.call_soon_threadsafe(self.notify_queue.put_nowait, job_dict)
        else:
            self.notify_queue.put_nowait(job_dict)

    async def _triage_worker(self, worker_id: int) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while self._running:
                try:
                    job = await self.triage_queue.get()
                except asyncio.CancelledError:
                    break
                jid = job.get("id", 0)
                try:
                    TASK_REGISTRY.set_job_stage(jid, f"System-1 Triage (Worker {worker_id})")
                    enriched = await score_single_job(client, job)
                    if enriched and enriched.get("triage_passed"):
                        self.enqueue_notify_job(enriched)
                except Exception as e:
                    logger.error("Error in triage worker %d on job #%s: %s", worker_id, jid, e)
                finally:
                    TASK_REGISTRY.clear_job_stage(jid)
                    self.triage_queue.task_done()

    async def _notify_worker(self) -> None:
        from src.hitl.telegram_bot import send_single_job_card

        while self._running:
            try:
                job = await self.notify_queue.get()
            except asyncio.CancelledError:
                break
            jid = job.get("id", 0)
            try:
                TASK_REGISTRY.set_job_stage(jid, "Sending Rich Discovery Card to Telegram")
                await send_single_job_card(job)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error("Error in notify worker on job #%s: %s", jid, e)
            finally:
                TASK_REGISTRY.clear_job_stage(jid)
                self.notify_queue.task_done()

    async def drain(self) -> None:
        """Awaits until currently queued triage and notify jobs are completely processed."""
        await self.triage_queue.join()
        await self.notify_queue.join()

    async def recover_state_on_startup(self) -> None:
        """
        Full Crash & Outage Resilience:
        Queries Neon DB for any jobs interrupted mid-discovery, mid-triage,
        mid-interest/tailoring, mid-revision, or mid-submission, and resumes them cleanly.
        """
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
            return

        def _fetch_interrupted():
            with get_connection() as conn, conn.cursor() as cur:
                # 1. Unscored jobs
                cur.execute(
                    "SELECT id, company, title, location, jd_text FROM jobs WHERE triage_passed IS NULL ORDER BY id ASC;"
                )
                unscored = [dict(r) for r in cur.fetchall()]

                # 2. Triaged but unnotified jobs
                cur.execute(
                    """
                    SELECT id, company, title, location, jd_text, jev_fit_prob, jev_fit_tier, jev_scam_prob, jev_persona
                    FROM jobs
                    WHERE triage_passed = TRUE AND human_action IS NULL AND notified_at IS NULL
                    ORDER BY id ASC;
                    """
                )
                unnotified = [dict(r) for r in cur.fetchall()]

                # 3. Interrupted PROCESSING
                cur.execute("SELECT * FROM jobs WHERE workflow_stage = 'PROCESSING' ORDER BY id ASC;")
                processing = [dict(r) for r in cur.fetchall()]

                # 4. Interrupted EDITING
                cur.execute("SELECT * FROM jobs WHERE workflow_stage = 'EDITING' ORDER BY id ASC;")
                editing = [dict(r) for r in cur.fetchall()]

                # 5. Interrupted SUBMITTING
                cur.execute("SELECT * FROM jobs WHERE workflow_stage = 'SUBMITTING' ORDER BY id ASC;")
                submitting = [dict(r) for r in cur.fetchall()]

                return unscored, unnotified, processing, editing, submitting

        try:
            unscored, unnotified, processing, editing, submitting = await asyncio.to_thread(_fetch_interrupted)
        except Exception as e:
            logger.warning("Could not query crash recovery jobs from DB: %s", e)
            return

        from src.hitl.telegram_bot import (
            resume_job_interest_after_crash,
            resume_job_submit_after_crash,
        )

        # 1. Unscored
        for j in unscored:
            self.enqueue_discovered_job(j)
        if unscored:
            logger.info("[Recovery] Enqueued %d unscored jobs for streaming triage", len(unscored))

        # 2. Triaged but unnotified
        for j in unnotified:
            self.enqueue_notify_job(j)
        if unnotified:
            logger.info("[Recovery] Enqueued %d triaged jobs for notification", len(unnotified))

        # 3. Interrupted PROCESSING
        for job_row in processing:
            logger.info("[Recovery] Resuming interrupted PROCESSING for Job #%s (%s)", job_row["id"], job_row["company"])
            asyncio.create_task(resume_job_interest_after_crash(job_row, is_edit=False))

        # 4. Interrupted EDITING
        for job_row in editing:
            notes = job_row.get("user_edit_notes") or []
            latest_note = notes[-1] if notes else ""
            logger.info(
                "[Recovery] Resuming interrupted EDITING for Job #%s (%s) with note: '%s'",
                job_row["id"],
                job_row["company"],
                latest_note,
            )
            asyncio.create_task(resume_job_interest_after_crash(job_row, custom_note=latest_note, is_edit=True))

        # 5. Interrupted SUBMITTING
        for job_row in submitting:
            logger.info("[Recovery] Safely resolving interrupted SUBMITTING for Job #%s (%s)", job_row["id"], job_row["company"])
            asyncio.create_task(resume_job_submit_after_crash(job_row))

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Wire write-through insertion hook
        set_job_inserted_hook(self.enqueue_discovered_job)

        # Recover interrupted states
        await self.recover_state_on_startup()

        # Start triage workers
        for wid in range(1, self.triage_concurrency + 1):
            t = asyncio.create_task(self._triage_worker(wid))
            self._tasks.append(t)

        # Start notify worker
        nt = asyncio.create_task(self._notify_worker())
        self._tasks.append(nt)

        logger.info("PipelineEngine started with %d triage workers and 1 notify worker", self.triage_concurrency)

    async def stop(self) -> None:
        self._running = False
        set_job_inserted_hook(None)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("PipelineEngine stopped")

    async def run(self) -> None:
        """Starts engine and keeps running until cancelled."""
        await self.start()
        try:
            while self._running:
                await asyncio.sleep(1.0)
        finally:
            await self.stop()
