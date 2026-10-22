import os
import re
import json
import math
import asyncio
import httpx
from collections import Counter
from dotenv import load_dotenv
from src.db import get_connection

load_dotenv()

TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

with open("config/master_profile.json", "r") as f:
    PROFILE = json.load(f)

SCORE_TIER_LABELS = ["reject", "weak_fit", "moderate_fit", "strong_fit", "dream_role"]

def compute_baseline_similarity(jd_text: str) -> float:
    """Computes a fast baseline cosine similarity between profile skills and JD text for the Eval Harness."""
    if not jd_text:
        return 0.0
    summary_str = PROFILE.get("experience_summary", "")
    profile_text = " ".join(PROFILE.get("core_skills", []) + [summary_str]).lower()
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

async def evaluate_with_jev(client: httpx.AsyncClient, job: dict) -> dict:
    """
    Calls Jev System-1 Decision Model via TypeSafe Direct (/v1/systemone)
    or OpenRouter Decisions API (/api/alpha/decisions) in a single parallel pass.
    """
    if TYPESAFE_API_KEY:
        url = "https://api.typesafe.ai/v1/systemone"
        api_key = TYPESAFE_API_KEY
        model_id = "jev-1.13.0"
    elif OPENROUTER_API_KEY:
        url = "https://openrouter.ai/api/alpha/decisions"
        api_key = OPENROUTER_API_KEY
        model_id = "typesafe/jev-1.14"
    else:
        raise ValueError("Set either TYPESAFE_API_KEY or OPENROUTER_API_KEY in your .env file!")

    payload = {
        "model": model_id,
        "state": {
            "candidate": {
                "location": PROFILE.get("location", "Hyderabad, India"),
                "core_skills": PROFILE.get("core_skills", []),
            },
            "job_listing": {
                "company": job["company"],
                "title": job["title"],
                "location": job["location"],
                "description": (job["jd_text"] or "")[:4500],
            },
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
                "instructions": "How strongly does this job match the candidate's Python, AI/ML, LLM, and Backend engineering profile (1 to 4 years experience)?",
                "criteria": [
                    "Unrelated role (e.g., iOS, Android, pure Frontend, Sales) or requires 8+ years Principal/Director seniority.",
                    "Weak match: shares only 1-2 peripheral keywords or wrong primary language (e.g., pure C++ embedded or .NET).",
                    "Moderate match: general software, data, or cloud engineering role with partial Python/SQL overlap.",
                    "Strong match: directly aligns with Python Backend, FastAPI, PostgreSQL, Data, or ML engineering at early-to-mid seniority.",
                    "Dream role: exact match for AI Engineer, Agentic LLM workflows, LangGraph, RAG, or Python Backend Engineer in India/Remote.",
                ],
            },
            "persona": {
                "type": "choice",
                "instructions": "Which candidate resume persona best matches this job description?",
                "criteria": {
                    "ai_agent_engineer": "Roles emphasizing LLMs, GenAI, Agentic workflows, LangGraph, RAG, or Machine Learning.",
                    "backend_python_engineer": "Roles emphasizing Python backend APIs, FastAPI, microservices, distributed systems, or cloud infrastructure.",
                    "data_ml_engineer": "Roles emphasizing data engineering, SQL, PostgreSQL, analytics, Streamlit dashboards, or ETL pipelines.",
                },
            },
        },
    }

    resp = await client.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Jev API returned {resp.status_code}: {resp.text}")

    answers = resp.json()["answers"]

    # 1. Extract Noul (0.0 to 1.0 probability of scam/harvester)
    scam_prob = float(answers["scam_or_harvester"]["noul"])

    # 2. Extract Score (0.0 to 4.0 position across the 5 ordered rubric levels)
    score_ans = answers["ats_fit_tier"]
    raw_score = float(score_ans["score"])  # 0.0 to 4.0
    level_idx = min(4, max(0, int(round(raw_score))))
    fit_tier = SCORE_TIER_LABELS[level_idx]

    # Convert 0..4 rubric score into a calibrated 0..1 fit probability
    probs = score_ans.get("probabilities", {})
    if probs:
        fit_prob = float(
            probs.get("3", probs.get(3, 0.0))
            + probs.get("4", probs.get(4, 0.0))
            + 0.5 * probs.get("2", probs.get(2, 0.0))
        )
    else:
        fit_prob = raw_score / 4.0

    # 3. Extract Choice (winning resume persona)
    persona = answers["persona"]["choice"]

    return {
        "jev_scam_prob": round(scam_prob, 4),
        "jev_fit_tier": fit_tier,
        "jev_fit_prob": round(min(1.0, max(0.0, fit_prob)), 4),
        "jev_persona": persona,
    }

async def run_triage(batch_limit: int = 25):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, company, title, location, jd_text FROM jobs WHERE triage_passed IS NULL LIMIT %s;",
                (batch_limit,),
            )
            unscored_jobs = cur.fetchall()

        if not unscored_jobs:
            print("✨ All jobs in Neon PostgreSQL are already triaged!")
            return

        route = "TypeSafe Direct (/v1/systemone)" if TYPESAFE_API_KEY else "OpenRouter Decisions API (/api/alpha/decisions)"
        print(f"🧠 Running System-1 Jev Triage on {len(unscored_jobs)} jobs via {route}...")

        passed_count = 0
        async with httpx.AsyncClient(timeout=25.0) as client:
            for job in unscored_jobs:
                cos_sim = compute_baseline_similarity(job["jd_text"])
                try:
                    res = await evaluate_with_jev(client, job)

                    # Confidence Gate: Scam probability under 0.30 AND Fit probability at least 0.55
                    passed = (res["jev_scam_prob"] < 0.30) and (res["jev_fit_prob"] >= 0.55)
                    if passed:
                        passed_count += 1

                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE jobs
                            SET cosine_sim = %s,
                                jev_scam_prob = %s,
                                jev_fit_tier = %s,
                                jev_fit_prob = %s,
                                jev_persona = %s,
                                triage_passed = %s
                            WHERE id = %s;
                            """,
                            (
                                cos_sim,
                                res["jev_scam_prob"],
                                res["jev_fit_tier"],
                                res["jev_fit_prob"],
                                res["jev_persona"],
                                passed,
                                job["id"],
                            ),
                        )
                    conn.commit()
                    icon = "✅ PASS" if passed else "🗑️ DROP"
                    print(
                        f"  {icon} | [{job['company']}] {job['title'][:42]} "
                        f"-> Fit: {res['jev_fit_prob']:.2f} ({res['jev_fit_tier']}) | Scam: {res['jev_scam_prob']:.2f}"
                    )
                except Exception as e:
                    print(f"  ⚠️ Error scoring job #{job['id']} ({job['company']}): {e}")

        print(f"\n🎯 Jev Triage finished! {passed_count}/{len(unscored_jobs)} jobs cleared the gate.")

if __name__ == "__main__":
    asyncio.run(run_triage())