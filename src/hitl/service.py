"""Channel-agnostic Human-in-the-Loop (HITL) service layer.

Provides a unified async/sync interface over Relentless jobs, resume tailoring,
Playwright application inspection/submission, OKF Knowledge Brain concepts,
evaluation statistics, and user configuration so Telegram, Web, and Discord
interfaces stay strictly synchronized.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from psycopg.types.json import Jsonb
import yaml

from evals.evaluate_triage import (
    brier_score,
    precision_at_k,
    run_evaluation,
    wilson_95_ci,
)
from src.apply.runner import submit_application
from src.config import AppConfig, invalidate_config, load_config
from src.db import create_apply_confirmation, get_connection, reset_database
from src.knowledge.distiller import run_distill
from src.knowledge.store import Concept, get_store, is_stale, trust_tier
from src.pipeline import TASK_REGISTRY
from src.settings_store import (
    LOCAL_CONFIG_PATH,
    LOCAL_PROFILE_PATH,
    _invalidate_profile_cache,
    load_master_profile,
)
from src.tailor.resume_tailor import ensure_tailored_pdf

logger = logging.getLogger(__name__)

SUPPORTED_SKIP_ACTIONS = {"SKIP_BAD_FIT", "SKIP_SCAM"}
SUPPORTED_OUTCOME_ACTIONS = {"CALLBACK", "INTERVIEW", "OFFER", "REJECTED"}
SUPPORTED_ACTIONS = SUPPORTED_SKIP_ACTIONS | SUPPORTED_OUTCOME_ACTIONS | {"MANUALLY_APPLIED"}

JOB_LIST_COLUMNS = """
    id, dedup_hash, source_ats, company, title, location, apply_url, discovered_at,
    cosine_sim, jev_scam_prob, jev_fit_tier, jev_fit_prob, jev_fit_prob_cal,
    jev_persona, triage_passed, human_action, human_action_at, outcome_status,
    workflow_stage, apply_status, apply_feasible, apply_blocker, notified_at,
    tailored_pdf_path, ats_scorecard
"""


def _serialize_value(val: Any) -> Any:
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_serialize_value(v) for v in val]
    return val


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if k == "embedding":
            continue
        out[k] = _serialize_value(v)
    return out


def _parse_pause_state(raw_value: Any) -> tuple[bool, str | None]:
    if not raw_value:
        return False, None
    until_dt: datetime | None = None
    if isinstance(raw_value, datetime):
        until_dt = raw_value
    elif isinstance(raw_value, str):
        try:
            until_dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return False, None
    if until_dt is None:
        return False, None
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=UTC)
    is_paused = datetime.now(UTC) < until_dt
    return is_paused, until_dt.isoformat() if is_paused else None


def _resolve_chat_id(chat_id: int | None = None, fallback_row_chat_id: int | None = None) -> int:
    if chat_id is not None:
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            pass
    if fallback_row_chat_id is not None:
        try:
            return int(fallback_row_chat_id)
        except (TypeError, ValueError):
            pass
    raw_cid = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    try:
        return int(raw_cid) if raw_cid else 0
    except ValueError:
        return 0


def _spawn_background_distill() -> None:
    async def _runner():
        try:
            await asyncio.to_thread(run_distill)
        except Exception as e:
            logger.warning("Background distillation failed: %s", e)

    try:
        asyncio.create_task(_runner())
    except RuntimeError:
        pass


# ==============================================================================
# 1. System Status & Telemetry
# ==============================================================================
def get_system_status() -> dict[str, Any]:
    """Combines database job counts, pause status, and live TASK_REGISTRY state."""
    total = 0
    passed = 0
    labeled = 0
    stage_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    is_paused = False
    paused_until: str | None = None

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM jobs;")
        total = int((cur.fetchone() or {}).get("c", 0))

        cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE triage_passed = TRUE;")
        passed = int((cur.fetchone() or {}).get("c", 0))

        cur.execute("SELECT COUNT(*) AS c FROM jobs WHERE human_action IS NOT NULL;")
        labeled = int((cur.fetchone() or {}).get("c", 0))

        cur.execute(
            "SELECT COALESCE(workflow_stage, 'DISCOVERED') AS stage, COUNT(*) AS c "
            "FROM jobs GROUP BY COALESCE(workflow_stage, 'DISCOVERED');"
        )
        for row in cur.fetchall() or []:
            stage_counts[str(row["stage"])] = int(row["c"])

        cur.execute(
            "SELECT human_action, COUNT(*) AS c FROM jobs "
            "WHERE human_action IS NOT NULL GROUP BY human_action;"
        )
        for row in cur.fetchall() or []:
            action_counts[str(row["human_action"])] = int(row["c"])

        cur.execute(
            "SELECT outcome_status, COUNT(*) AS c FROM jobs "
            "WHERE outcome_status IS NOT NULL GROUP BY outcome_status;"
        )
        for row in cur.fetchall() or []:
            outcome_counts[str(row["outcome_status"])] = int(row["c"])

        cur.execute("SELECT value FROM app_settings WHERE key = 'automation_paused_until';")
        pause_row = cur.fetchone()
        if pause_row:
            is_paused, paused_until = _parse_pause_state(pause_row.get("value"))

    reg = TASK_REGISTRY.get_all_status()
    from src.readiness import collect_readiness

    readiness = collect_readiness(check_db=False)
    readiness["db_ok"] = True
    return {
        "total_jobs": total,
        "triage_passed": passed,
        "human_labeled": labeled,
        "stage_counts": stage_counts,
        "action_counts": action_counts,
        "outcome_counts": outcome_counts,
        "active_jobs": dict(reg.get("active_jobs", {})),
        "system_tasks": list(reg.get("system_tasks", [])),
        "is_paused": is_paused,
        "paused_until": paused_until,
        "readiness": readiness,
    }


async def async_get_system_status() -> dict[str, Any]:
    return await asyncio.to_thread(get_system_status)


# ==============================================================================
# 2. Jobs Listing & Detail Inspection
# ==============================================================================
def list_jobs(
    stage: str | None = None,
    fit_tier: str | None = None,
    source_ats: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Queries jobs with high-level pipeline stage presets and enriches in-flight stage."""
    conditions: list[str] = []
    params: list[Any] = []

    if stage and stage.strip():
        st = stage.strip().upper()
        if st in ("UNREVIEWED", "INBOX"):
            conditions.append("triage_passed = TRUE AND human_action IS NULL")
        elif st == "IN_PROGRESS":
            conditions.append(
                "workflow_stage IN ('PROCESSING', 'EDITING', 'AWAITING_EDIT_NOTE', 'SUBMITTING')"
            )
        elif st == "READY_FOR_REVIEW":
            conditions.append("workflow_stage = 'READY_FOR_REVIEW'")
        elif st == "APPLIED":
            conditions.append("(workflow_stage = 'APPLIED' OR human_action = 'APPLIED')")
        elif st == "CALLBACK":
            conditions.append(
                "(workflow_stage = 'CALLBACK' OR outcome_status IN ('CALLBACK', 'INTERVIEW', 'OFFER'))"
            )
        elif st == "SKIPPED":
            conditions.append("human_action IN ('SKIP_BAD_FIT', 'SKIP_SCAM')")
        else:
            conditions.append("workflow_stage = %s")
            params.append(stage.strip())

    if fit_tier and fit_tier.strip():
        conditions.append("jev_fit_tier = %s")
        params.append(fit_tier.strip())

    if source_ats and source_ats.strip():
        conditions.append("source_ats = %s")
        params.append(source_ats.strip())

    if search and search.strip():
        like_term = f"%{search.strip()}%"
        conditions.append("(company ILIKE %s OR title ILIKE %s OR location ILIKE %s)")
        params.extend([like_term, like_term, like_term])

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit_val = max(1, min(int(limit), 200))
    offset_val = max(0, int(offset))

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM jobs {where_sql};", tuple(params))
        total_count = int((cur.fetchone() or {}).get("c", 0))

        cur.execute(
            f"""
            SELECT {JOB_LIST_COLUMNS}
            FROM jobs
            {where_sql}
            ORDER BY COALESCE(jev_fit_prob_cal, jev_fit_prob, 0) DESC, id DESC
            LIMIT %s OFFSET %s;
            """,
            tuple(params + [limit_val, offset_val]),
        )
        rows = cur.fetchall() or []

    items: list[dict[str, Any]] = []
    for raw in rows:
        item = _serialize_row(raw)
        if item is None:
            continue
        jid = item["id"]
        pdf_path = item.get("tailored_pdf_path")
        item["in_flight_stage"] = TASK_REGISTRY.get_job_stage(jid)
        item["has_pdf"] = bool(pdf_path and Path(str(pdf_path)).exists())
        items.append(item)

    return {
        "items": items,
        "total": total_count,
        "limit": limit_val,
        "offset": offset_val,
    }


async def async_list_jobs(
    stage: str | None = None,
    fit_tier: str | None = None,
    source_ats: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        list_jobs,
        stage=stage,
        fit_tier=fit_tier,
        source_ats=source_ats,
        search=search,
        limit=limit,
        offset=offset,
    )


def get_job_detail(job_id: int) -> dict[str, Any] | None:
    """Returns full job detail, apply/outcome events, screenshots, and live stage."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
        job_raw = cur.fetchone()
        if not job_raw:
            return None

        cur.execute(
            "SELECT * FROM apply_events WHERE job_id = %s ORDER BY started_at DESC LIMIT 10;",
            (job_id,),
        )
        apply_rows = cur.fetchall() or []

        cur.execute(
            "SELECT * FROM outcome_events WHERE job_id = %s ORDER BY occurred_at DESC LIMIT 10;",
            (job_id,),
        )
        outcome_rows = cur.fetchall() or []

    job = _serialize_row(job_raw)
    assert job is not None

    apply_events = [_serialize_row(r) for r in apply_rows if r is not None]
    outcome_events = [_serialize_row(r) for r in outcome_rows if r is not None]

    seen_shots: set[str] = set()
    screenshots: list[str] = []

    for ev in apply_events:
        for sp in ev.get("screenshots") or []:
            sp_str = str(sp)
            if sp_str not in seen_shots and Path(sp_str).exists():
                seen_shots.add(sp_str)
                screenshots.append(sp_str)

    evidence_dir = Path("output") / "apply_evidence" / str(job_id)
    if evidence_dir.is_dir():
        for p in sorted(evidence_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                p_str = str(p)
                if p_str not in seen_shots:
                    seen_shots.add(p_str)
                    screenshots.append(p_str)

    pdf_path = job.get("tailored_pdf_path")
    has_pdf = bool(
        (pdf_path and Path(str(pdf_path)).exists()) or job.get("tailored_resume_json")
    )

    job["apply_events"] = apply_events
    job["outcome_events"] = outcome_events
    job["screenshots"] = screenshots
    job["has_pdf"] = has_pdf
    job["in_flight_stage"] = TASK_REGISTRY.get_job_stage(job_id)
    return job


async def async_get_job_detail(job_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(get_job_detail, job_id)


# ==============================================================================
# 3. Job Lifecycle Actions (Interest / Edit / Submit / Decisions)
# ==============================================================================
async def start_job_interest(
    job_id: int,
    custom_note: str = "",
    is_edit: bool = False,
    actor_source: str = "web",
    chat_id: int | None = None,
    clicked_mid: int | None = None,
) -> dict[str, Any]:
    """Starts resume tailoring + website inspection for a job, or applies a resume edit note."""
    in_flight = TASK_REGISTRY.get_job_stage(job_id)
    if in_flight:
        return {
            "ok": False,
            "error": "BUSY",
            "in_flight_stage": in_flight,
            "job_id": job_id,
        }

    from src.hitl import telegram_bot as tg_bot

    target_chat_id = _resolve_chat_id(chat_id=chat_id)
    note_clean = (custom_note or "").strip()

    if is_edit and note_clean:
        await asyncio.to_thread(
            tg_bot._append_user_edit_note,
            job_id,
            note_clean,
            target_chat_id or None,
        )

        def _fetch_job():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
                row = cur.fetchone()
                return dict(row) if row else None

        job_row = await asyncio.to_thread(_fetch_job)
    else:
        def _mark_processing():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET human_action = 'APPLIED',
                        human_action_at = NOW(),
                        workflow_stage = 'PROCESSING',
                        active_chat_id = COALESCE(%s, active_chat_id)
                    WHERE id = %s
                    RETURNING *;
                    """,
                    (target_chat_id or None, job_id),
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None

        job_row = await asyncio.to_thread(_mark_processing)

    if not job_row:
        return {"ok": False, "error": "NOT_FOUND", "job_id": job_id}

    if target_chat_id == 0 and job_row.get("active_chat_id"):
        target_chat_id = _resolve_chat_id(fallback_row_chat_id=job_row.get("active_chat_id"))

    _spawn_background_distill()
    asyncio.create_task(
        tg_bot._process_job_interest(
            job_row,
            target_chat_id,
            custom_note=note_clean,
            is_edit=is_edit,
            clicked_mid=clicked_mid,
        )
    )

    return {
        "ok": True,
        "job_id": job_id,
        "workflow_stage": "EDITING" if is_edit else "PROCESSING",
        "actor_source": actor_source,
    }


async def record_job_action(
    job_id: int,
    action: str,
    actor_source: str = "web",
    chat_id: int | None = None,
    clicked_mid: int | None = None,
) -> dict[str, Any]:
    """Records Skip / Manual Apply / Outcome updates and synchronizes Telegram card state."""
    action_norm = (action or "").strip().upper()
    if action_norm not in SUPPORTED_ACTIONS:
        raise ValueError(f"Unsupported job action: {action}")

    def _persist_action():
        with get_connection() as conn, conn.cursor() as cur:
            if action_norm in SUPPORTED_SKIP_ACTIONS:
                cur.execute(
                    """
                    UPDATE jobs
                    SET human_action = %s,
                        human_action_at = NOW(),
                        workflow_stage = 'REVIEWED'
                    WHERE id = %s
                    RETURNING *;
                    """,
                    (action_norm, job_id),
                )
                row = cur.fetchone()
            elif action_norm == "MANUALLY_APPLIED":
                cur.execute(
                    """
                    UPDATE jobs
                    SET human_action = 'APPLIED',
                        human_action_at = NOW(),
                        apply_status = 'MANUALLY_APPLIED',
                        workflow_stage = 'APPLIED'
                    WHERE id = %s
                    RETURNING *;
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
            else:
                cur.execute(
                    """
                    UPDATE jobs
                    SET outcome_status = %s,
                        workflow_stage = CASE
                            WHEN %s IN ('CALLBACK', 'INTERVIEW', 'OFFER') THEN 'CALLBACK'
                            ELSE workflow_stage
                        END
                    WHERE id = %s
                    RETURNING *;
                    """,
                    (action_norm, action_norm, job_id),
                )
                row = cur.fetchone()
                if row is not None:
                    cur.execute(
                        "INSERT INTO outcome_events (job_id, kind, source) VALUES (%s, %s, %s);",
                        (job_id, action_norm, actor_source),
                    )
            conn.commit()
            return dict(row) if row else None

    updated_row = await asyncio.to_thread(_persist_action)
    if not updated_row:
        return {"ok": False, "error": "NOT_FOUND", "job_id": job_id}

    _spawn_background_distill()

    target_chat_id = _resolve_chat_id(
        chat_id=chat_id,
        fallback_row_chat_id=updated_row.get("active_chat_id"),
    )
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if target_chat_id and bot_token:
        try:
            from src.hitl import telegram_bot as tg_bot

            async with httpx.AsyncClient(timeout=15.0) as client:
                if action_norm in SUPPORTED_SKIP_ACTIONS:
                    await tg_bot.cleanup_job_messages(
                        client, target_chat_id, job_id, extra_mid=clicked_mid
                    )
                elif action_norm == "MANUALLY_APPLIED":
                    co_name = updated_row.get("company") or f"Job #{job_id}"
                    await tg_bot.upsert_job_rich_card(
                        client=client,
                        chat_id=target_chat_id,
                        job_id=job_id,
                        markdown_body=(
                            f"🎉 *Job #{job_id} ({co_name})* — Marked as applied manually "
                            f"({actor_source}). Best of luck!"
                        ),
                        try_edit_text_only=True,
                        clicked_mid=clicked_mid,
                    )
                else:
                    await tg_bot.upsert_job_rich_card(
                        client=client,
                        chat_id=target_chat_id,
                        job_id=job_id,
                        markdown_body=f"⭐ *Job #{job_id}* marked as: `{action_norm}` ({actor_source})!",
                        try_edit_text_only=True,
                        clicked_mid=clicked_mid,
                    )
        except Exception as e:
            logger.warning("Telegram cross-channel sync failed for job #%s: %s", job_id, e)

    return {
        "ok": True,
        "job_id": job_id,
        "action": action_norm,
        "job": _serialize_row(updated_row),
    }


async def execute_job_submit(
    job_id: int,
    token: str | None = None,
    actor_source: str = "web",
    chat_id: int | None = None,
    clicked_mid: int | None = None,
) -> dict[str, Any]:
    """Executes human-confirmed application submission and syncs workflow stage and Telegram."""
    in_flight = TASK_REGISTRY.get_job_stage(job_id)
    if in_flight:
        return {
            "ok": False,
            "error": "BUSY",
            "in_flight_stage": in_flight,
            "job_id": job_id,
        }

    def _set_submitting():
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET workflow_stage = 'SUBMITTING' WHERE id = %s RETURNING *;",
                (job_id,),
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None

    job_row = await asyncio.to_thread(_set_submitting)
    if not job_row:
        return {"ok": False, "error": "NOT_FOUND", "job_id": job_id}

    if not token:
        token, _ = await asyncio.to_thread(
            create_apply_confirmation,
            job_id,
            kind="SUBMIT",
            payload={
                "pdf_sha256": job_row.get("tailored_pdf_sha256"),
                "apply_url": job_row.get("apply_url"),
            },
            ttl_seconds=1800,
            created_by=actor_source,
        )

    TASK_REGISTRY.set_job_stage(job_id, "Submitting application via browser")
    try:
        res = await submit_application(job_row, token)
        status = res.get("status")
        is_success = status == "SUBMITTED"
        final_stage = "APPLIED" if is_success else "READY_FOR_REVIEW"

        def _mark_done():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET workflow_stage = %s WHERE id = %s;",
                    (final_stage, job_id),
                )
                conn.commit()

        await asyncio.to_thread(_mark_done)

        target_chat_id = _resolve_chat_id(
            chat_id=chat_id,
            fallback_row_chat_id=job_row.get("active_chat_id"),
        )
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if target_chat_id and bot_token:
            try:
                from src.hitl import telegram_bot as tg_bot

                err_txt = res.get("error") or ""
                icon = "🚀" if is_success else "⚠️"
                result_text = (
                    f"{icon} *Auto-Submit Result for Job #{job_id} ({job_row['company']}):* *{status}*\n\n"
                    f"{err_txt}\n"
                    f"🔗 [Application Page]({job_row['apply_url']})"
                )
                sub_shots = [s for s in (res.get("screenshots") or []) if Path(s).exists()]
                proof_img = sub_shots[-1] if sub_shots else None
                pdf_p = await ensure_tailored_pdf(job_row)
                buttons = None
                if not is_success:
                    buttons = [
                        [{"text": "🔄 Retry / Review", "callback_data": f"retry_submit:{job_id}"}],
                        [{"text": "✅ Mark Applied Manually", "callback_data": f"manual_applied:{job_id}"}],
                    ]
                async with httpx.AsyncClient(timeout=30.0) as client:
                    await tg_bot.upsert_job_rich_card(
                        client=client,
                        chat_id=target_chat_id,
                        job_id=job_id,
                        markdown_body=result_text,
                        inline_keyboard=buttons,
                        screenshot_path=proof_img,
                        pdf_path=pdf_p,
                        clicked_mid=clicked_mid,
                    )
            except Exception as e:
                logger.warning("Telegram sync after submit failed for job #%s: %s", job_id, e)

        return {
            "ok": is_success,
            "job_id": job_id,
            "workflow_stage": final_stage,
            **res,
        }
    except Exception as e:
        def _rollback_stage():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET workflow_stage = 'READY_FOR_REVIEW' WHERE id = %s;",
                    (job_id,),
                )
                conn.commit()

        try:
            await asyncio.to_thread(_rollback_stage)
        except Exception:
            pass
        return {
            "ok": False,
            "job_id": job_id,
            "status": "FAILED",
            "error": str(e),
            "workflow_stage": "READY_FOR_REVIEW",
        }
    finally:
        TASK_REGISTRY.clear_job_stage(job_id)


# ==============================================================================
# 4. OKF Knowledge Brain Helpers
# ==============================================================================
def _serialize_concept(c: Concept) -> dict[str, Any]:
    return {
        "path": c.path,
        "concept_type": c.concept_type,
        "name": c.name,
        "description": c.description,
        "resource": c.resource,
        "tags": list(c.tags or []),
        "status": c.status,
        "trust_tier": trust_tier(c),
        "is_stale": is_stale(c),
        "stale_after": c.stale_after,
        "body": c.body,
        "updated_at": c.updated_at,
    }


def get_okf_brain_data(query: str = "", concept_type: str | None = None) -> dict[str, Any]:
    """Returns serialized OKF concepts and summary insights."""
    store = get_store()
    if not getattr(store, "_hydrated", False):
        store.hydrate()

    if query or concept_type:
        matches = store.search(query=query, type=concept_type)
    else:
        matches = sorted(store.concepts.values(), key=lambda c: c.path)

    serialized = [_serialize_concept(c) for c in matches]

    hot = store.get("market/hot-skills")
    vel = store.get("market/company-hiring-velocity")
    trends = store.get("market/india-remote-trends")
    edits = store.get("preferences/resume-edit-habits")
    scams = store.search("", type="Scam Pattern")

    summary = {
        "hot_skills": hot.body if hot else "",
        "hiring_velocity": vel.body if vel else "",
        "remote_trends": trends.body if trends else "",
        "edit_habits": edits.body if edits else "",
        "scam_count": len(scams),
        "total_concepts": len(store.concepts),
    }

    return {"concepts": serialized, "summary": summary}


async def async_get_okf_brain_data(
    query: str = "", concept_type: str | None = None
) -> dict[str, Any]:
    return await asyncio.to_thread(get_okf_brain_data, query=query, concept_type=concept_type)


def _normalize_okf_path(raw_path: str) -> str:
    clean = (raw_path or "").strip().strip("/").removesuffix(".md")
    if not clean or ".." in clean.split("/"):
        raise ValueError(f"Invalid OKF concept path: {raw_path!r}")
    if clean in ("index", "log") or clean.endswith("/index"):
        raise ValueError(f"Reserved OKF concept path: {raw_path!r}")
    return clean


def save_okf_concept(
    path: str,
    concept_type: str,
    body: str,
    name: str = "",
    description: str = "",
    tags: list[str] | None = None,
    actor: str = "human:owner",
) -> dict[str, Any]:
    """Upserts an OKF concept and flushes to Neon PostgreSQL and Markdown."""
    clean_path = _normalize_okf_path(path)
    if not concept_type or not concept_type.strip():
        raise ValueError("concept_type is required")

    store = get_store()
    if not getattr(store, "_hydrated", False):
        store.hydrate()

    existing = store.get(clean_path)
    verified = list(existing.verified) if existing and existing.verified else []
    if actor.startswith("human:") and not any(
        isinstance(v, dict) and v.get("by") == actor for v in verified
    ):
        verified.append({"by": actor, "at": datetime.now(UTC).isoformat()})

    frontmatter = dict(existing.frontmatter) if existing and existing.frontmatter else {}
    frontmatter["type"] = concept_type.strip()
    if name:
        frontmatter["title"] = name
    if description:
        frontmatter["description"] = description
    if tags is not None:
        frontmatter["tags"] = list(tags)

    concept = Concept(
        path=clean_path,
        concept_type=concept_type.strip(),
        name=name or (existing.name if existing else clean_path.split("/")[-1]),
        description=description or (existing.description if existing else ""),
        resource=existing.resource if existing else "",
        tags=list(tags) if tags is not None else (list(existing.tags) if existing else []),
        frontmatter=frontmatter,
        verified=verified,
        status=existing.status if existing else "stable",
        stale_after=existing.stale_after if existing else None,
        body=body,
    )
    store.upsert(concept, actor=actor)
    store.flush()
    return _serialize_concept(concept)


async def async_save_okf_concept(
    path: str,
    concept_type: str,
    body: str,
    name: str = "",
    description: str = "",
    tags: list[str] | None = None,
    actor: str = "human:owner",
) -> dict[str, Any]:
    return await asyncio.to_thread(
        save_okf_concept,
        path=path,
        concept_type=concept_type,
        body=body,
        name=name,
        description=description,
        tags=tags,
        actor=actor,
    )


def delete_okf_concept(path: str) -> dict[str, Any]:
    clean_path = _normalize_okf_path(path)
    store = get_store()
    if not getattr(store, "_hydrated", False):
        store.hydrate()
    existed = store.get(clean_path) is not None
    store.delete(clean_path)
    store.flush()
    return {"ok": True, "path": clean_path, "deleted": existed}


async def async_delete_okf_concept(path: str) -> dict[str, Any]:
    return await asyncio.to_thread(delete_okf_concept, path)


# ==============================================================================
# 5. ML Evaluation & Calibration Metrics
# ==============================================================================
def get_evaluation_data() -> dict[str, Any]:
    """Computes structured Precision@K, Wilson 95% CI, Brier calibration, and ATS breakdown."""
    total_ingested = 0
    total_passed = 0
    labeled_rows: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM jobs;")
        total_ingested = int((cur.fetchone() or {}).get("total", 0))

        cur.execute("SELECT COUNT(*) AS passed FROM jobs WHERE triage_passed = TRUE;")
        total_passed = int((cur.fetchone() or {}).get("passed", 0))

        cur.execute(
            """
            SELECT id, company, title, source_ats, cosine_sim, jev_fit_prob, jev_fit_prob_cal,
                   jev_scam_prob, jev_fit_tier, human_action, outcome_status
            FROM jobs
            WHERE human_action IN ('APPLIED', 'SKIP_BAD_FIT', 'SKIP_SCAM')
            ORDER BY human_action_at DESC;
            """
        )
        labeled_rows = [dict(r) for r in (cur.fetchall() or [])]

        try:
            cur.execute(
                "SELECT id, ran_at, n, metrics, config_version "
                "FROM eval_runs ORDER BY ran_at DESC LIMIT 20;"
            )
            history = [_serialize_row(r) for r in (cur.fetchall() or []) if r is not None]
        except Exception:
            history = []

    n = len(labeled_rows)
    if n == 0:
        try:
            report_md = run_evaluation()
        except Exception:
            report_md = "No ground-truth labels yet."
        return {
            "total_ingested": total_ingested,
            "total_passed": total_passed,
            "n": 0,
            "total_applied": 0,
            "total_skipped": 0,
            "callbacks": 0,
            "overall_precision": 0.0,
            "overall_ci": [0.0, 0.0],
            "k": 0,
            "jev_precision_at_k": 0.0,
            "jev_hits": 0,
            "jev_ci": [0.0, 0.0],
            "baseline_precision_at_k": 0.0,
            "baseline_hits": 0,
            "baseline_ci": [0.0, 0.0],
            "brier_jev": 0.0,
            "brier_base": 0.0,
            "brier_skill_score": None,
            "source_breakdown": {},
            "history": history,
            "report_markdown": report_md,
        }

    y_true = [1 if r.get("human_action") == "APPLIED" else 0 for r in labeled_rows]
    jev_probs = [float(r.get("jev_fit_prob") or 0.0) for r in labeled_rows]
    base_probs = [float(r.get("cosine_sim") or 0.0) for r in labeled_rows]

    total_applied = sum(y_true)
    callbacks = sum(
        1 for r in labeled_rows if r.get("outcome_status") in ("CALLBACK", "INTERVIEW", "OFFER")
    )

    overall_prec = total_applied / n
    ci_low, ci_high = wilson_95_ci(total_applied, n)

    jev_ranked_y = [
        1 if r.get("human_action") == "APPLIED" else 0
        for r in sorted(labeled_rows, key=lambda x: float(x.get("jev_fit_prob") or 0.0), reverse=True)
    ]
    base_ranked_y = [
        1 if r.get("human_action") == "APPLIED" else 0
        for r in sorted(labeled_rows, key=lambda x: float(x.get("cosine_sim") or 0.0), reverse=True)
    ]

    k_val = min(10, n)
    jev_p_k, jev_hits, k_eff = precision_at_k(jev_ranked_y, k_val)
    base_p_k, base_hits, _ = precision_at_k(base_ranked_y, k_val)
    jev_k_low, jev_k_high = wilson_95_ci(jev_hits, k_eff)
    base_k_low, base_k_high = wilson_95_ci(base_hits, k_eff)

    bs_jev = brier_score(jev_probs, y_true)
    bs_base = brier_score(base_probs, y_true)
    base_rate = total_applied / n
    bs_climatology = sum((base_rate - y) ** 2 for y in y_true) / n
    bss: float | None = (1.0 - (bs_jev / bs_climatology)) if bs_climatology > 0 else None

    source_breakdown: dict[str, dict[str, Any]] = {}
    for r in labeled_rows:
        src = str(r.get("source_ats") or "unknown")
        bucket = source_breakdown.setdefault(src, {"total": 0, "applied": 0, "skipped": 0})
        bucket["total"] += 1
        if r.get("human_action") == "APPLIED":
            bucket["applied"] += 1
        else:
            bucket["skipped"] += 1
    for bucket in source_breakdown.values():
        bucket["approval_rate"] = bucket["applied"] / bucket["total"] if bucket["total"] else 0.0

    try:
        report_md = run_evaluation()
    except Exception:
        report_md = ""

    return {
        "total_ingested": total_ingested,
        "total_passed": total_passed,
        "n": n,
        "total_applied": total_applied,
        "total_skipped": n - total_applied,
        "callbacks": callbacks,
        "overall_precision": overall_prec,
        "overall_ci": [ci_low, ci_high],
        "k": k_eff,
        "jev_precision_at_k": jev_p_k,
        "jev_hits": jev_hits,
        "jev_ci": [jev_k_low, jev_k_high],
        "baseline_precision_at_k": base_p_k,
        "baseline_hits": base_hits,
        "baseline_ci": [base_k_low, base_k_high],
        "brier_jev": bs_jev,
        "brier_base": bs_base,
        "brier_skill_score": bss,
        "source_breakdown": source_breakdown,
        "history": history,
        "report_markdown": report_md,
    }


async def async_get_evaluation_data() -> dict[str, Any]:
    return await asyncio.to_thread(get_evaluation_data)


# ==============================================================================
# 6. User Profile, Configuration & Automation Controls
# ==============================================================================
def get_user_settings() -> dict[str, Any]:
    """Reads master_profile, config_yaml, effective config, pause state, and readiness."""
    from src.readiness import collect_readiness

    profile_error: str | None = None
    try:
        profile = load_master_profile()
    except Exception as e:
        profile = {}
        profile_error = str(e)

    raw_config: dict[str, Any] = {}
    is_paused = False
    paused_until: str | None = None

    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'config_yaml';")
            cfg_row = cur.fetchone()
            if cfg_row and isinstance(cfg_row.get("value"), dict):
                raw_config = dict(cfg_row["value"])

            cur.execute("SELECT value FROM app_settings WHERE key = 'automation_paused_until';")
            pause_row = cur.fetchone()
            if pause_row:
                is_paused, paused_until = _parse_pause_state(pause_row.get("value"))
    except Exception:
        pass

    if not raw_config and os.path.exists(LOCAL_CONFIG_PATH):
        try:
            with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw_config = yaml.safe_load(f) or {}
        except Exception:
            raw_config = {}

    try:
        effective_cfg = load_config(force=True).model_dump()
    except Exception:
        effective_cfg = raw_config

    readiness = collect_readiness(check_db=False)
    return {
        "master_profile": profile,
        "profile_configured": readiness["profile_configured"],
        "profile_error": profile_error or readiness.get("profile_error"),
        "config": raw_config or effective_cfg,
        "effective_config": effective_cfg,
        "is_paused": is_paused,
        "paused_until": paused_until,
        "readiness": readiness,
    }


async def async_get_user_settings() -> dict[str, Any]:
    return await asyncio.to_thread(get_user_settings)


def save_master_profile(profile_dict: dict[str, Any]) -> dict[str, Any]:
    """Persists master_profile into Neon app_settings (and local JSON if present) and clears cache."""
    if not isinstance(profile_dict, dict):
        raise TypeError("master_profile must be a JSON object")

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('master_profile', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
            """,
            (Jsonb(profile_dict),),
        )
        conn.commit()

    if os.path.exists(LOCAL_PROFILE_PATH):
        import json

        with open(LOCAL_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(profile_dict, f, indent=2, ensure_ascii=False)

    _invalidate_profile_cache()
    return {"ok": True, "master_profile": profile_dict}


async def async_save_master_profile(profile_dict: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(save_master_profile, profile_dict)


def save_config_yaml(config_dict: dict[str, Any]) -> dict[str, Any]:
    """Validates AppConfig, persists config_yaml into Neon app_settings (and local YAML if present), and clears cache."""
    if not isinstance(config_dict, dict):
        raise TypeError("config must be a mapping/object")

    validated = AppConfig(**config_dict).model_dump()

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('config_yaml', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
            """,
            (Jsonb(validated),),
        )
        conn.commit()

    if os.path.exists(LOCAL_CONFIG_PATH):
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(validated, f, sort_keys=False, allow_unicode=True)

    invalidate_config()
    return {"ok": True, "config": validated}


async def async_save_config_yaml(config_dict: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(save_config_yaml, config_dict)


def set_automation_pause(hours: float | None = 24.0) -> dict[str, Any]:
    """Pauses or resumes automated job submission for the given duration in hours."""
    with get_connection() as conn, conn.cursor() as cur:
        if hours is None or float(hours) <= 0:
            cur.execute("DELETE FROM app_settings WHERE key = 'automation_paused_until';")
            conn.commit()
            return {"ok": True, "is_paused": False, "paused_until": None}

        until = datetime.now(UTC) + timedelta(hours=float(hours))
        cur.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('automation_paused_until', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
            """,
            (Jsonb(until.isoformat()),),
        )
        conn.commit()
        return {"ok": True, "is_paused": True, "paused_until": until.isoformat()}


async def async_set_automation_pause(hours: float | None = 24.0) -> dict[str, Any]:
    return await asyncio.to_thread(set_automation_pause, hours)


def reset_database_action(mode: str) -> dict[str, Any]:
    """Safely executes database reset ('labels', 'triage', or 'all')."""
    msg = reset_database(mode)
    return {"ok": True, "mode": mode, "message": msg}


async def async_reset_database_action(mode: str) -> dict[str, Any]:
    return await asyncio.to_thread(reset_database_action, mode)
