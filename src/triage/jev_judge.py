from __future__ import annotations

import asyncio
from collections import Counter
import logging
import math
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
import httpx

from src.calibrate import (
    apply_calibration,
    fit_and_persist_calibration_model,
    load_active_calibration_model,
)
from src.config import load_config
from src.db import get_connection
from src.embeddings import semantic_search_concepts
from src.httpx_utils import post_with_retry
from src.jd_parse import parse_jd
from src.knowledge.store import get_store
from src.model_catalog import resolve_triage_endpoint
from src.notify import alert
from src.settings_store import get_master_profile

load_dotenv()

TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

logger = logging.getLogger(__name__)

SCORE_TIER_LABELS = ["reject", "weak_fit", "moderate_fit", "strong_fit", "dream_role"]


def _build_jd_description(job: dict, profile: dict) -> str:
    jd_text = job.get("jd_text") or ""
    structured = parse_jd(jd_text, profile)
    parts = []
    if structured.get("salary_mentions"):
        parts.append("Compensation: " + ", ".join(structured["salary_mentions"]))
    if structured.get("skills"):
        parts.append("Skills: " + ", ".join(structured["skills"][:35]))
    if structured.get("requirements"):
        parts.append("Requirements: " + structured["requirements"][:2500])
    if structured.get("responsibilities"):
        parts.append("Responsibilities: " + structured["responsibilities"][:2500])
    summary = "\n".join(parts)
    return (summary + "\n\n" + jd_text[:12000]).strip()


def compute_baseline_similarity(jd_text: str) -> float:
    """Fast lexical baseline cosine similarity retained for ablation benchmarking vs Jev & embeddings."""
    if not jd_text:
        return 0.0
    profile = get_master_profile()
    summary_str = profile.get("experience_summary", "")
    profile_text = " ".join(profile.get("core_skills", []) + [summary_str]).lower()
    words_a = re.findall(r"[a-z0-9+#]{2,}", profile_text)
    words_b = re.findall(r"[a-z0-9+#]{2,}", jd_text.lower())
    if not words_a or not words_b:
        return 0.0
    vec_a, vec_b = Counter(words_a), Counter(words_b)
    common = set(vec_a.keys()) & set(vec_b.keys())
    dot = sum(vec_a[w] * vec_b[w] for w in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    return round(dot / (norm_a * norm_b), 4) if (norm_a and norm_b) else 0.0


def _summarize_candidate_for_jev(profile: dict[str, Any], cfg) -> dict[str, Any]:
    app_defaults = profile.get("application_defaults") or {}
    exp_years = app_defaults.get("total_experience_years") or app_defaults.get("relevant_experience_years") or ""
    exp_brief = [
        {
            "company": e.get("company", ""),
            "role": e.get("role", ""),
            "dates": e.get("dates", ""),
            "highlights": (e.get("bullets") or [])[:3],
        }
        for e in (profile.get("experience") or [])[:4]
    ]
    policy = cfg.effective_location_policy()
    return {
        "location": profile.get("location", ""),
        "total_experience_years": exp_years,
        "experience_summary": profile.get("experience_summary", ""),
        "core_skills": profile.get("core_skills", []),
        "experience": exp_brief,
        "target_roles": cfg.candidate_preferences.target_role_keywords[:20],
        "excluded_titles": cfg.candidate_preferences.exclude_title_keywords,
        "location_policy": {
            "operator": policy.operator,
            "candidate_country": policy.candidate_country,
            "reject_countries": policy.reject_countries,
            "rules": [r.model_dump(exclude_defaults=True) for r in policy.rules],
        },
    }


async def retrieve_relevant_okf_context(
    job: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    top_k: int = 5,
) -> str:
    """Semantic RAG over OKF Brain concepts so Jev sees ONLY knowledge relevant to this job."""
    try:
        store = get_store()
        all_concepts = [
            c
            for c in store.concepts.values()
            if c.concept_type in ("Scam Pattern", "Candidate Preference", "Company Note", "Strategy")
        ]
        if not all_concepts:
            return ""
        query_text = f"{job.get('company', '')} {job.get('title', '')} {job.get('location', '')}\n{(job.get('jd_text') or '')[:2500]}"
        matched = await semantic_search_concepts(query_text, all_concepts, top_k=top_k, client=client)
        return "\n\n".join(f"[{c.concept_type} — {c.name}]\n{c.body}" for c in matched)
    except Exception:
        return ""


async def evaluate_with_jev(
    client: httpx.AsyncClient,
    job: dict[str, Any],
    learned_context: str | None = None,
) -> dict[str, Any]:
    """Calls Jev System-1 Decision Model via configured provider (OpenRouter Decisions or TypeSafe Direct)."""
    cfg = load_config()
    url, api_key, model_id = resolve_triage_endpoint(cfg.models.triage.provider, cfg.models.triage.model)
    if not url or not api_key:
        raise ValueError("Set either TYPESAFE_API_KEY or OPENROUTER_API_KEY in your .env file!")

    profile = get_master_profile()
    cand_state = _summarize_candidate_for_jev(profile, cfg)
    exp_years_label = cand_state["total_experience_years"] or "mid-level"

    if learned_context is None:
        learned_context = await retrieve_relevant_okf_context(job, client=client)

    payload = {
        "model": model_id,
        "state": {
            "candidate": cand_state,
            "job_listing": {
                "company": job["company"],
                "title": job["title"],
                "location": job["location"],
                "work_mode": job.get("work_mode") or "unknown",
                "description": _build_jd_description(job, profile),
            },
            "learned_context": learned_context,
        },
        "questions": {
            "scam_or_harvester": {
                "type": "noul",
                "instructions": "Is this job listing likely a ghost job, resume farm, unpaid homework trap, or mandatory AI-interview biometric/voice data harvester?",
                "criteria": {
                    "true": "Requires instant automated AI video/voice recording prior to human review, unpaid trial project, sensitive ID, or vague stealth staffing agency.",
                    "false": "Legitimate engineering role at a verifiable technology product company or enterprise GCC with standard hiring stages.",
                },
            },
            "ats_fit_tier": {
                "type": "score",
                "instructions": (
                    f"How strongly does this job match the candidate's verified experience ({exp_years_label} years), "
                    f"technical stack, target roles, and location_policy?"
                ),
                "criteria": [
                    "Unrelated role (e.g., Intern, iOS, Android, pure Frontend, Sales), wrong geo/work-mode, or requires far higher seniority (8+ yrs Principal/Director).",
                    "Weak match: shares only 1-2 peripheral keywords or wrong primary stack (e.g., pure C++ embedded, Java enterprise, or .NET).",
                    "Moderate match: general software, data, or cloud engineering role with partial Python/ML/SQL overlap.",
                    "Strong match: directly aligns with candidate's ML/AI, Computer Vision, LLM, or Python Backend stack and seniority level.",
                    "Dream role: exact match for candidate's core strengths (GenAI/Agentic LLMs, Computer Vision/Edge AI, Python Backend/MLOps) and location_policy.",
                ],
            },
            "persona": {
                "type": "choice",
                "instructions": "Which candidate resume persona best matches this job description?",
                "criteria": {
                    "ai_agent_engineer": "Roles emphasizing LLMs, GenAI, Agentic workflows, LangGraph, RAG, VLMs, or Applied Machine Learning.",
                    "backend_python_engineer": "Roles emphasizing Python backend APIs, FastAPI, microservices, distributed systems, or cloud/MLOps infrastructure.",
                    "data_ml_engineer": "Roles emphasizing data engineering, SQL, PostgreSQL, analytics, Computer Vision pipelines, or ETL.",
                },
            },
        },
    }

    resp = await post_with_retry(
        client,
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Jev API returned {resp.status_code}: {resp.text}")

    answers = resp.json()["answers"]

    scam_prob = float(answers["scam_or_harvester"]["noul"])
    score_ans = answers["ats_fit_tier"]
    raw_score = float(score_ans["score"])
    level_idx = min(4, max(0, int(round(raw_score))))
    fit_tier = SCORE_TIER_LABELS[level_idx]

    probs = score_ans.get("probabilities", {})
    if probs:
        fit_prob = float(
            probs.get("3", probs.get(3, 0.0))
            + probs.get("4", probs.get(4, 0.0))
            + 0.5 * probs.get("2", probs.get(2, 0.0))
        )
    else:
        fit_prob = raw_score / 4.0

    persona = answers["persona"]["choice"]

    return {
        "jev_scam_prob": round(scam_prob, 4),
        "jev_fit_tier": fit_tier,
        "jev_fit_prob": round(min(1.0, max(0.0, fit_prob)), 4),
        "jev_persona": persona,
    }


_CAL_CACHE: dict[str, Any] = {"model": None, "ts": 0.0}
_CAL_LOCK = asyncio.Lock()


def _fetch_or_refresh_calibration_sync():
    active = load_active_calibration_model()
    if active is not None:
        return active
    return fit_and_persist_calibration_model(min_n=30, min_per_class=10)


async def get_cached_calibration_model(ttl_seconds: float = 300.0):
    now = time.monotonic()
    if _CAL_CACHE["model"] is not None and (now - _CAL_CACHE["ts"]) < ttl_seconds:
        return _CAL_CACHE["model"]
    async with _CAL_LOCK:
        if _CAL_CACHE["model"] is not None and (now - _CAL_CACHE["ts"]) < ttl_seconds:
            return _CAL_CACHE["model"]
        model = await asyncio.to_thread(_fetch_or_refresh_calibration_sync)
        _CAL_CACHE["model"] = model
        _CAL_CACHE["ts"] = now
        return model


async def score_single_job(client: httpx.AsyncClient, job: dict) -> dict | None:
    """Executes LangGraph TriageGraph (Embed -> OKF Semantic RAG -> Jev -> Calibration -> Persist)."""
    from src.orchestrator.graph import run_triage_graph

    try:
        return await run_triage_graph(job=job, http_client=client)
    except Exception as e:
        logger.error("Error scoring job #%s (%s): %s", job.get("id"), job.get("company"), e)
        alert(f"⚠️ Error scoring job #{job.get('id')} ({job.get('company')}): {e}")
        return None


def compute_calibrated_rank_score(cal_model, raw_fit: float) -> float:
    return round(apply_calibration(cal_model, raw_fit), 4) if cal_model else raw_fit


async def run_triage(batch_limit: int = 100):
    def _fetch_unscored():
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM jobs WHERE triage_passed IS NULL LIMIT %s;",
                (batch_limit,),
            )
            return cur.fetchall()

    unscored_jobs = await asyncio.to_thread(_fetch_unscored)
    if not unscored_jobs:
        print("✨ All jobs in Neon PostgreSQL are already triaged!")
        return

    cfg = load_config()
    print(
        f"🧠 Running LangGraph TriageGraph ({cfg.models.triage.provider}/{cfg.models.triage.model}) on {len(unscored_jobs)} jobs..."
    )

    sem = asyncio.Semaphore(cfg.ingestion.triage_concurrency)
    passed_count = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        async def _bound_score(job):
            nonlocal passed_count
            async with sem:
                enriched = await score_single_job(client, job)
                if enriched and enriched.get("triage_passed"):
                    passed_count += 1

        await asyncio.gather(*[_bound_score(job) for job in unscored_jobs])

    print(f"\n🎯 LangGraph Triage finished! {passed_count}/{len(unscored_jobs)} jobs cleared the gate.")


if __name__ == "__main__":
    asyncio.run(run_triage())
