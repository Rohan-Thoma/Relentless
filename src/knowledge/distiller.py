from collections import Counter
import logging
import re

from src.db import get_connection
from src.knowledge.store import Concept, get_store, process_actor

logger = logging.getLogger(__name__)

REDACT = True

HOT_TECH_TERMS = [
    "langgraph",
    "langchain",
    "rag",
    "tensorrt",
    "yolov8",
    "yolo",
    "opencv",
    "fastapi",
    "pytorch",
    "tensorflow",
    "docker",
    "aws",
    "bedrock",
    "langsmith",
    "langfuse",
    "mlflow",
    "airflow",
    "postgresql",
    "pgvector",
    "deepstream",
    "jetson",
    "onnx",
    "vlm",
    "transformers",
    "agentic",
    "llm",
    "genai",
    "computer vision",
    "mlops",
    "python",
]


def _redact(text: str) -> str:
    if not REDACT:
        return text
    text = re.sub(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", "[email]", text)
    text = re.sub(r"\+?\d[\d\s-]{8,}\d", "[phone]", text)
    return text


def distill_user_feedback(store) -> int:
    """Distills human actions (APPLIED, SKIP_BAD_FIT, SKIP_SCAM) and user_edit_notes into OKF concepts."""
    count = 0
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, company, title, human_action, jev_persona
                FROM jobs
                WHERE human_action IS NOT NULL
                ORDER BY human_action_at DESC
                LIMIT 100;
                """
            )
            labeled_rows = cur.fetchall()

            cur.execute(
                """
                SELECT id, company, title, user_edit_notes
                FROM jobs
                WHERE user_edit_notes IS NOT NULL
                  AND jsonb_typeof(user_edit_notes) = 'array'
                  AND jsonb_array_length(user_edit_notes) > 0
                ORDER BY id DESC
                LIMIT 50;
                """
            )
            edit_rows = cur.fetchall()

            cur.execute("SELECT pattern FROM knowledge_forget;")
            forgotten = {r["pattern"] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not query DB for user feedback distillation: %s", e)
        return 0

    scams = []
    bad_fits = []
    applied = []

    for row in labeled_rows:
        comp = row["company"]
        if any(f in comp for f in forgotten):
            continue
        action = row["human_action"]
        if action == "SKIP_SCAM":
            scams.append(row)
        elif action == "SKIP_BAD_FIT":
            bad_fits.append(row)
        elif action == "APPLIED":
            applied.append(row)

    # 1. Distill individual scams
    for s in scams:
        slug = re.sub(r"[^a-z0-9]+", "-", s["company"].lower()).strip("-")
        path = f"patterns/scam-{slug}"
        body = _redact(
            f"Scam / ghost listing pattern identified for company: {s['company']}.\n"
            f"Title: {s['title']} (Job #{s['id']}).\n"
            f"Triage guidance: Reject immediate automated interviews, unpaid test tasks, or deceptive staffing agencies."
        )
        c = Concept(
            path=path,
            concept_type="Scam Pattern",
            name=f"Scam: {s['company']}",
            body=body,
            status="stable",
            tags=["scam", "ghost-job"],
        )
        store.upsert(c, actor=process_actor("distiller"))
        count += 1

    # 2. Distill aggregate bad-fit preferences
    if bad_fits:
        sample_bad = bad_fits[:15]
        companies = sorted({b["company"] for b in sample_bad})
        titles = sorted({b["title"] for b in sample_bad})
        body = (
            "Candidate rejected the following job types and organizations as poor fit:\n\n"
            f"• Sample Companies: {', '.join(companies[:10])}\n"
            f"• Sample Titles: {', '.join(titles[:10])}\n\n"
            "System-1 Triage Guidance: Down-weight jobs matching these patterns unless high alignment is explicitly proven."
        )
        c = Concept(
            path="preferences/rejected-patterns",
            concept_type="Candidate Preference",
            name="Rejected Roles and Bad Fits",
            body=body,
            status="stable",
            tags=["preference", "negative-signal"],
        )
        store.upsert(c, actor=process_actor("distiller"))
        count += 1

    # 3. Distill aggregate approved preferences
    if applied:
        sample_app = applied[:15]
        companies = sorted({a["company"] for a in sample_app})
        titles = sorted({a["title"] for a in sample_app})
        personas = [a.get("jev_persona") for a in sample_app if a.get("jev_persona")]
        persona_counts = Counter(personas)
        body = (
            "Candidate enthusiastically targeted and applied to the following roles:\n\n"
            f"• Priority Titles: {', '.join(titles[:10])}\n"
            f"• Target Companies: {', '.join(companies[:10])}\n"
            f"• Winning Personas: {dict(persona_counts)}\n\n"
            "System-1 Triage Guidance: Prioritize similar AI/ML and engineering roles."
        )
        c = Concept(
            path="preferences/approved-patterns",
            concept_type="Candidate Preference",
            name="Target Candidate Preferences",
            body=body,
            status="stable",
            tags=["preference", "positive-signal"],
        )
        store.upsert(c, actor=process_actor("distiller"))
        count += 1

    # 4. Distill resume edit habits from user_edit_notes
    all_notes = []
    for er in edit_rows:
        notes = er.get("user_edit_notes") or []
        if isinstance(notes, list):
            for n in notes:
                if n and isinstance(n, str):
                    all_notes.append(f"[{er['company']} - {er['title']}] {n.strip()}")

    if all_notes:
        notes_body = "\n".join(f"• {n}" for n in all_notes[:20])
        body = (
            "Recurring User Instructions for Resume Tailoring:\n\n"
            f"{notes_body}\n\n"
            "Tailoring Guidance: Incorporate these preferred framings (e.g., edge optimization, LangSmith tracing, specific architecture details) automatically during System-2 tailoring."
        )
        c = Concept(
            path="preferences/resume-edit-habits",
            concept_type="Resume Win",
            name="User Resume Customization Habits",
            body=body,
            status="stable",
            tags=["resume-win", "user-edits"],
        )
        store.upsert(c, actor=process_actor("distiller"))
        count += 1

    return count


def distill_market_intelligence(store) -> int:
    """Computes real-time market trends across all scraped jobs in Neon PostgreSQL."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_ats, company, title, location, jd_text
                FROM jobs
                ORDER BY id DESC
                LIMIT 500;
                """
            )
            jobs = cur.fetchall()
    except Exception as e:
        logger.warning("Could not query DB for market intelligence: %s", e)
        return 0

    if not jobs:
        return 0

    total_jobs = len(jobs)
    tech_counts = Counter()
    company_counts = Counter()
    ats_counts = Counter()
    location_counts = Counter()

    for j in jobs:
        comp = j.get("company") or "Unknown"
        ats = j.get("source_ats") or "other"
        loc = (j.get("location") or "Remote").lower()
        company_counts[comp] += 1
        ats_counts[ats] += 1

        # Normalize location buckets
        if any(w in loc for w in ("hyderabad", "secunderabad", "telangana")):
            location_counts["Hyderabad"] += 1
        elif any(w in loc for w in ("bangalore", "bengaluru", "karnataka")):
            location_counts["Bengaluru"] += 1
        elif any(w in loc for w in ("pune", "mumbai", "maharashtra")):
            location_counts["Pune / Mumbai"] += 1
        elif any(w in loc for w in ("delhi", "gurgaon", "gurugram", "noida", "ncr")):
            location_counts["NCR (Delhi/Gurgaon/Noida)"] += 1
        elif any(w in loc for w in ("chennai", "tamil nadu")):
            location_counts["Chennai"] += 1
        elif any(w in loc for w in ("remote", "anywhere", "worldwide", "global", "wfh")):
            location_counts["Remote / Anywhere"] += 1
        else:
            location_counts["Other India / International"] += 1

        full_text = f"{(j.get('title') or '')} {(j.get('jd_text') or '')}".lower()
        for term in HOT_TECH_TERMS:
            if re.search(r"\b" + re.escape(term) + r"\b", full_text):
                tech_counts[term] += 1

    # 1. Concept: market/hot-skills
    top_skills = tech_counts.most_common(20)
    skills_lines = [
        f"• `{skill}`: mentioned in {cnt} of {total_jobs} jobs ({cnt / total_jobs * 100:.1f}%)"
        for skill, cnt in top_skills
    ]
    body_skills = (
        f"# AI/ML & Tech Hot Skills Intelligence (Sample Size: {total_jobs} active postings)\n\n"
        + "\n".join(skills_lines)
    )
    store.upsert(
        Concept(
            path="market/hot-skills",
            concept_type="Market Intelligence",
            name="In-Demand AI & Engineering Skills",
            body=body_skills,
            status="stable",
            tags=["market", "skills", "demand"],
        ),
        actor=process_actor("distiller"),
    )

    # 2. Concept: market/company-hiring-velocity
    top_cos = company_counts.most_common(15)
    cos_lines = [f"• **{co}**: {cnt} open matching roles" for co, cnt in top_cos]
    body_velocity = (
        f"# Company Hiring Velocity Report\n\n"
        f"**Active ATS Platforms:** {dict(ats_counts.most_common())}\n\n"
        f"**Top Hiring Companies:**\n" + "\n".join(cos_lines)
    )
    store.upsert(
        Concept(
            path="market/company-hiring-velocity",
            concept_type="Market Intelligence",
            name="Company Hiring Velocity",
            body=body_velocity,
            status="stable",
            tags=["market", "velocity", "employers"],
        ),
        actor=process_actor("distiller"),
    )

    # 3. Concept: market/india-remote-trends
    loc_lines = [
        f"• **{loc}**: {cnt} jobs ({cnt / total_jobs * 100:.1f}%)" for loc, cnt in location_counts.most_common()
    ]
    body_loc = (
        f"# India & Remote Geographic Distribution ({total_jobs} roles analysed)\n\n" + "\n".join(loc_lines)
    )
    store.upsert(
        Concept(
            path="market/india-remote-trends",
            concept_type="Market Intelligence",
            name="Geographic & Remote Hiring Trends",
            body=body_loc,
            status="stable",
            tags=["market", "india", "remote"],
        ),
        actor=process_actor("distiller"),
    )

    return 3


def run_distill() -> dict:
    """Full background learner that updates the OKF Brain."""
    store = get_store()
    store.hydrate()
    fb_count = distill_user_feedback(store)
    mkt_count = distill_market_intelligence(store)
    store.flush()
    return {"feedback_concepts": fb_count, "market_concepts": mkt_count}


if __name__ == "__main__":
    res = run_distill()
    print(f"Distillation complete: {res}")
