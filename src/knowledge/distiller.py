"""OKF Brain Distiller with Intelligent Skip-Reason Learning Gate & Meta-Intent Edit Extraction.

- Distinguishes durable candidate preferences (experience mismatch, wrong stack, wrong location, scam)
  from ephemeral noise (expired link, busy today) so the brain NEVER learns bad signals.
- Distills rejection patterns by REASON & ROLE PATTERN instead of blacklisting entire companies.
- Extracts generalizable tailoring intent (the WHY behind user resume edit instructions).
"""

from __future__ import annotations

from collections import Counter
import logging
import os
import re

import httpx

from src.config import load_config
from src.db import get_connection
from src.knowledge.store import Concept, get_store, process_actor
from src.model_catalog import resolve_chat_endpoint

logger = logging.getLogger(__name__)

REDACT = True

# Structured skip reasons supported by Web UI, Telegram, and HITL service
SKIP_REASON_CATEGORIES: dict[str, dict] = {
    "BAD_MATCH_EXPERIENCE": {"label": "Experience / Seniority Mismatch (e.g. Intern or 8+ yrs)", "default_learn": True},
    "BAD_MATCH_SKILLS": {"label": "Wrong Tech Stack / Domain", "default_learn": True},
    "BAD_MATCH_LOCATION": {"label": "Wrong Location / Not Truly Remote", "default_learn": True},
    "EXPIRED_OR_CLOSED": {"label": "Job Link Already Expired / Closed", "default_learn": False},
    "SCAM_OR_BAD_COMPANY": {"label": "Scam / Ghost / Avoid Company", "default_learn": True},
    "BUSY_OR_OTHER": {"label": "Busy / Personal / Custom Reason", "default_learn": False},
}

EPHEMERAL_NOISE_PATTERNS = (
    r"\bexpired\b",
    r"\bclosed\b",
    r"\bno longer accepting\b",
    r"\b404\b",
    r"\bbroken link\b",
    r"\bbusy\b",
    r"\blater\b",
    r"\balready applied\b",
    r"\bduplicate\b",
    r"\baccidental\b",
)

DURABLE_PREF_PATTERNS = (
    r"\bintern\b",
    r"\bsenior\b",
    r"\bjunior\b",
    r"\byears?\b",
    r"\bexperience\b",
    r"\bonsite\b",
    r"\bhybrid\b",
    r"\bremote\b",
    r"\brelocat\b",
    r"\bsalary\b",
    r"\bctc\b",
    r"\bpay\b",
    r"\bstack\b",
    r"\bjava\b",
    r"\bfrontend\b",
    r"\bconsulting\b",
    r"\bservice[- ]based\b",
    r"\bscam\b",
    r"\bghost\b",
    r"\bunpaid\b",
    r"\bavoid\b",
    r"\bprefer\b",
    r"\bdon'?t want\b",
    r"\bnot interested in\b",
)

HOT_TECH_TERMS = [
    "langgraph", "langchain", "rag", "tensorrt", "yolov8", "yolo", "opencv",
    "fastapi", "pytorch", "tensorflow", "docker", "aws", "bedrock", "langsmith",
    "langfuse", "mlflow", "airflow", "postgresql", "pgvector", "deepstream",
    "jetson", "onnx", "vlm", "transformers", "agentic", "llm", "genai",
    "computer vision", "mlops", "python",
]


def _redact(text: str) -> str:
    if not REDACT:
        return text
    text = re.sub(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", "[email]", text)
    text = re.sub(r"\+?\d[\d\s-]{8,}\d", "[phone]", text)
    return text


def should_learn_from_skip(
    action: str,
    reason_category: str | None = None,
    reason_note: str | None = None,
) -> tuple[bool, str]:
    """Intelligent Learning Gate: decides whether a skip/reject event carries a durable preference
    worth teaching the OKF Brain & calibration model, vs. ephemeral noise (expired link / busy).
    """
    cat = (reason_category or "").strip().upper()
    note = (reason_note or "").strip().lower()

    if cat == "EXPIRED_OR_CLOSED":
        return False, "Ephemeral: job listing expired or closed — excluded from preference learning."

    if cat in ("BAD_MATCH_EXPERIENCE", "BAD_MATCH_SKILLS", "BAD_MATCH_LOCATION", "SCAM_OR_BAD_COMPANY"):
        return True, f"Durable signal ({cat}) — recorded in OKF Brain."

    if action == "SKIP_SCAM":
        return True, "Scam/ghost pattern — recorded in OKF Brain."

    # For BUSY_OR_OTHER or uncategorized skips with a custom note: inspect intent!
    if note:
        has_durable = any(re.search(p, note) for p in DURABLE_PREF_PATTERNS)
        has_ephemeral = any(re.search(p, note) for p in EPHEMERAL_NOISE_PATTERNS)
        if has_durable:
            return True, "Custom note contains durable role/preference criteria — recorded in OKF Brain."
        if has_ephemeral:
            return False, "Custom note indicates transient/expired state — skipped learning."
        # Non-trivial custom explanation defaults to learn
        if len(note.split()) >= 4:
            return True, "Detailed custom skip feedback — recorded in OKF Brain."

    if cat == "BUSY_OR_OTHER":
        return False, "Transient skip without durable preference note — skipped learning."

    # Legacy bare SKIP_BAD_FIT without reason category still counts as mild preference signal
    return True, "Standard bad-fit skip — recorded."


def _extract_meta_edit_rules(notes: list[str]) -> list[str]:
    """Extracts generalizable tailoring principles (the WHY) from raw user edit notes.

    Uses configured LLM when available in production; deterministic grouping offline/in tests.
    """
    if not notes:
        return []
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            cfg = load_config()
            url, api_key, model = resolve_chat_endpoint(cfg.models.tailor.provider, cfg.models.tailor.model)
            if url and api_key:
                prompt = (
                    "Analyze these user resume edit instructions from multiple job applications. "
                    "Distill 3 to 5 GENERALIZABLE resume-tailoring principles describing WHY the user asks for these edits "
                    "(e.g., preferred technical depth, architecture framing, observability/metrics emphasis, skill positioning) "
                    "so future resumes improve automatically across ALL roles—not just repeating single tool names.\n\n"
                    "Instructions:\n" + "\n".join(f"- {n}" for n in notes[:25]) + "\n\n"
                    "Return plain bullet lines starting with '- '."
                )
                with httpx.Client(timeout=15.0) as client:
                    resp = client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.2,
                        },
                    )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    lines = [ln.strip().lstrip("-•* ").strip() for ln in text.splitlines() if ln.strip()]
                    if lines:
                        return lines[:6]
        except Exception as e:
            logger.debug("LLM meta-intent edit distillation skipped: %s", e)

    # Deterministic fallback grouping by intent theme
    themes: list[str] = []
    joined = " ".join(notes).lower()
    if any(k in joined for k in ("observability", "langsmith", "langfuse", "monitoring", "tracing", "telemetry")):
        themes.append("Foreground production observability, tracing, and evaluation tooling when tailoring AI/LLM roles.")
    if any(k in joined for k in ("edge", "latency", "fps", "tensorrt", "onnx", "optimiz", "throughput")):
        themes.append("Emphasize concrete inference latency, edge optimization, and hardware deployment benchmarks.")
    if any(k in joined for k in ("architect", "system", "scale", "backend", "microservice", "end-to-end")):
        themes.append("Frame accomplishments around end-to-end system ownership and production architecture trade-offs.")
    if any(k in joined for k in ("add", "include", "mention", "highlight")):
        themes.append("Actively surface relevant adjacent/requested ecosystem tools directly in both skills and project bullets.")
    if not themes:
        themes.append("Honor explicit candidate framing preferences and preserve technical depth across all roles.")
    return themes


def distill_user_feedback(store) -> int:
    """Distills durable human actions, structured skip reasons, and resume edit intent into OKF concepts."""
    count = 0
    try:
        with get_connection() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT id, company, title, location, human_action, jev_persona,
                           skip_reason_category, skip_reason_note, learn_from_action
                    FROM jobs
                    WHERE human_action IS NOT NULL
                    ORDER BY human_action_at DESC
                    LIMIT 150;
                    """
                )
            except Exception:
                if hasattr(conn, "rollback"):
                    conn.rollback()
                cur.execute(
                    """
                    SELECT id, company, title, human_action, jev_persona
                    FROM jobs
                    WHERE human_action IS NOT NULL
                    ORDER BY human_action_at DESC
                    LIMIT 150;
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
        cat = row.get("skip_reason_category")
        note = row.get("skip_reason_note")
        explicit_learn = row.get("learn_from_action")

        if action == "APPLIED":
            applied.append(row)
            continue

        # Apply Intelligent Learning Gate: skip expired links / busy noise!
        if explicit_learn is False:
            continue
        if explicit_learn is None:
            should_learn, _ = should_learn_from_skip(action, cat, note)
            if not should_learn:
                continue

        if action == "SKIP_SCAM" or cat == "SCAM_OR_BAD_COMPANY":
            scams.append(row)
        elif action == "SKIP_BAD_FIT":
            bad_fits.append(row)

    # 1. Distill individual verified scam/avoid patterns
    for s in scams:
        slug = re.sub(r"[^a-z0-9]+", "-", s["company"].lower()).strip("-")
        path = f"patterns/scam-{slug}"
        reason_line = f"User Note: {s['skip_reason_note']}\n" if s.get("skip_reason_note") else ""
        body = _redact(
            f"Scam / avoid-company pattern flagged for: {s['company']}.\n"
            f"Role: {s['title']} (Job #{s['id']}).\n"
            f"{reason_line}"
            f"Triage guidance: Reject deceptive listings, unpaid trial traps, or flagged avoid-list employers."
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

    # 2. Distill bad-fit patterns BY REASON & ROLE PATTERN (NEVER blanket-blacklist whole companies!)
    if bad_fits:
        by_reason: dict[str, list[str]] = {}
        for b in bad_fits[:25]:
            cat = b.get("skip_reason_category") or "GENERAL_MISMATCH"
            note_part = f" — \"{b['skip_reason_note']}\"" if b.get("skip_reason_note") else ""
            loc_part = f" ({b['location']})" if b.get("location") else ""
            by_reason.setdefault(cat, []).append(f"{b['title']}{loc_part}{note_part}")

        sections = [
            "Candidate rejected specific role patterns (do NOT blanket-penalize an entire company unless flagged as Scam; focus strictly on the role/seniority/location/stack mismatch below):\n"
        ]
        for cat, items in by_reason.items():
            label = SKIP_REASON_CATEGORIES.get(cat, {}).get("label", cat)
            sections.append(f"### {label}")
            for it in items[:8]:
                sections.append(f"- {_redact(it)}")
        sections.append(
            "\nSystem-1 Triage Guidance: Down-weight roles matching these seniority, stack, or location mismatches."
        )
        c = Concept(
            path="preferences/rejected-patterns",
            concept_type="Candidate Preference",
            name="Rejected Role Patterns & Reasons",
            body="\n".join(sections),
            status="stable",
            tags=["preference", "negative-signal"],
        )
        store.upsert(c, actor=process_actor("distiller"))
        count += 1

    # 3. Distill aggregate approved preferences
    if applied:
        sample_app = applied[:20]
        companies = sorted({a["company"] for a in sample_app})
        titles = sorted({a["title"] for a in sample_app})
        personas = [a.get("jev_persona") for a in sample_app if a.get("jev_persona")]
        persona_counts = Counter(personas)
        body = (
            "Candidate enthusiastically targeted and applied to the following roles:\n\n"
            f"• Priority Titles: {', '.join(titles[:12])}\n"
            f"• Sample Employers: {', '.join(companies[:12])}\n"
            f"• Winning Personas: {dict(persona_counts)}\n\n"
            "System-1 Triage Guidance: Prioritize similar AI/ML, LLM, Computer Vision, and Backend engineering roles."
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

    # 4. Distill meta-intent resume edit habits (the WHY, not just raw 1-off notes!)
    all_notes = []
    for er in edit_rows:
        notes = er.get("user_edit_notes") or []
        if isinstance(notes, list):
            for n in notes:
                if n and isinstance(n, str):
                    all_notes.append(f"[{er['company']} - {er['title']}] {n.strip()}")

    if all_notes:
        meta_principles = _extract_meta_edit_rules(all_notes)
        principles_block = "\n".join(f"- {p}" for p in meta_principles)
        notes_body = "\n".join(f"• {_redact(n)}" for n in all_notes[:15])
        body = (
            "## Learned Tailoring Principles (Meta-Intent)\n"
            f"{principles_block}\n\n"
            "## Recent Explicit User Edit Requests\n"
            f"{notes_body}\n"
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
    """Computes real-time market trends across scraped jobs in Neon PostgreSQL."""
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

        if any(w in loc for w in ("remote", "anywhere", "worldwide", "global", "wfh")):
            location_counts["Remote / Anywhere"] += 1
        elif any(w in loc for w in ("bangalore", "bengaluru", "karnataka")):
            location_counts["Bengaluru"] += 1
        elif any(w in loc for w in ("gurgaon", "gurugram", "noida", "delhi", "ncr")):
            location_counts["NCR (Delhi/Gurugram/Noida)"] += 1
        else:
            primary = (j.get("location") or "Unspecified").split(",")[0].strip().title()
            location_counts[primary or "Unspecified"] += 1

        full_text = f"{(j.get('title') or '')} {(j.get('jd_text') or '')}".lower()
        for term in HOT_TECH_TERMS:
            if re.search(r"\b" + re.escape(term) + r"\b", full_text):
                tech_counts[term] += 1

    top_skills = tech_counts.most_common(20)
    skills_lines = [
        f"• `{skill}`: mentioned in {cnt} of {total_jobs} jobs ({cnt / total_jobs * 100:.1f}%)"
        for skill, cnt in top_skills
    ]
    store.upsert(
        Concept(
            path="market/hot-skills",
            concept_type="Market Intelligence",
            name="In-Demand AI & Engineering Skills",
            body=f"# AI/ML & Tech Hot Skills Intelligence (Sample Size: {total_jobs} active postings)\n\n" + "\n".join(skills_lines),
            status="stable",
            tags=["market", "skills", "demand"],
        ),
        actor=process_actor("distiller"),
    )

    top_cos = company_counts.most_common(15)
    cos_lines = [f"• **{co}**: {cnt} open matching roles" for co, cnt in top_cos]
    store.upsert(
        Concept(
            path="market/company-hiring-velocity",
            concept_type="Market Intelligence",
            name="Company Hiring Velocity",
            body=f"# Company Hiring Velocity Report\n\n**Active ATS Platforms:** {dict(ats_counts.most_common())}\n\n**Top Hiring Companies:**\n" + "\n".join(cos_lines),
            status="stable",
            tags=["market", "velocity", "employers"],
        ),
        actor=process_actor("distiller"),
    )

    loc_lines = [
        f"• **{loc}**: {cnt} jobs ({cnt / total_jobs * 100:.1f}%)" for loc, cnt in location_counts.most_common()
    ]
    store.upsert(
        Concept(
            path="market/india-remote-trends",
            concept_type="Market Intelligence",
            name="Geographic & Remote Hiring Trends",
            body=f"# Geographic & Remote Distribution ({total_jobs} roles analysed)\n\n" + "\n".join(loc_lines),
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
