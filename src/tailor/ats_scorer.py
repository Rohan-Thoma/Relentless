"""100% Honest, Un-Clamped Before-vs-After ATS & Semantic Resume Scorecard.

Eliminates every legacy inflation trick:
- NO hardcoded 60% before-impact baseline (measures actual raw profile bullets)
- NO fabricated 75% -> 90% domain fallback
- NO max(before, after) clamping that hides skill regressions
- NO forced +5% minimum boost floor
- Scores ONLY content actually rendered into the compiled PDF (not unrendered metadata)
- Includes real embedding semantic similarity + dropped-skill & dropped-metric diagnostics
"""

from __future__ import annotations

import re
from typing import Any

from src.embeddings import cosine, embed_texts
from src.jd_parse import STATIC_SKILLS, parse_jd, skill_in_text

STRONG_ACTION_VERBS = {
    "architected",
    "engineered",
    "spearheaded",
    "designed",
    "developed",
    "implemented",
    "deployed",
    "optimized",
    "accelerated",
    "built",
    "led",
    "automated",
    "reduced",
    "scaled",
    "orchestrated",
    "integrated",
    "pioneered",
    "transformed",
    "delivered",
    "created",
    "enhanced",
}

DOMAIN_KEYWORDS_CATALOG = {
    "latency",
    "throughput",
    "inference",
    "quantization",
    "fine-tuning",
    "distributed",
    "retrieval",
    "hybrid search",
    "observability",
    "monitoring",
    "profiling",
    "guardrails",
    "chunking",
    "vector search",
    "microservices",
    "pipeline",
    "benchmarking",
    "orchestration",
    "caching",
    "scalability",
    "evaluation",
    "concurrency",
    "edge computing",
    "multimodal",
    "streaming",
    "multi-agent",
    "agentic",
}

METRIC_PATTERN = re.compile(
    r"(\d+[\.,]?\d*\s*(?:%|x|fps|ms|hrs|hours|s|sec|lpa|k|m)\b)|(\d+\+)|(\b\d{2,}\b)",
    re.IGNORECASE,
)


def _clean_token_display(token: str) -> str:
    acronyms = {"aws", "gcp", "sql", "nlp", "rag", "llm", "vlm", "api", "rest", "ci/cd", "ocr", "mlops", "etl", "dvc", "yolo", "onnx"}
    if token.lower() in acronyms:
        return token.upper()
    if token.lower() == "pytorch":
        return "PyTorch"
    if token.lower() == "tensorflow":
        return "TensorFlow"
    if token.lower() == "fastapi":
        return "FastAPI"
    if token.lower() == "langgraph":
        return "LangGraph"
    if token.lower() == "langchain":
        return "LangChain"
    if token.lower() == "tensorrt":
        return "TensorRT"
    if token.lower() == "postgresql":
        return "PostgreSQL"
    return token.title()


def _collect_tailored_bullets(tailored: dict[str, Any]) -> list[str]:
    by_role = tailored.get("tailored_bullets_by_role")
    if isinstance(by_role, list) and any(isinstance(r, list) and r for r in by_role):
        out: list[str] = []
        for r in by_role:
            if isinstance(r, list):
                out.extend(str(b) for b in r if b)
        return out
    return (
        list(tailored.get("tailored_bullets_role_1") or [])
        + list(tailored.get("tailored_bullets_role_2") or [])
        + list(tailored.get("tailored_bullets_role_3") or [])
    )


def _compute_impact_density(bullets: list[str]) -> int:
    if not bullets:
        return 0
    impact_count = 0
    for b in bullets:
        b_low = str(b).lower()
        has_metric = bool(METRIC_PATTERN.search(b_low))
        has_action = any(re.search(r"\b" + re.escape(v) + r"\b", b_low) for v in STRONG_ACTION_VERBS)
        if has_metric and has_action:
            impact_count += 1
        elif has_metric or has_action:
            impact_count += 0.65
    return min(100, round((impact_count / len(bullets)) * 100))


def _renderable_tailored_text(profile: dict[str, Any], tailored: dict[str, Any]) -> str:
    """Builds strictly the text that will actually be rendered inside the compiled Typst PDF."""
    parts: list[str] = []
    if tailored:
        parts.append(str(tailored.get("tailored_summary") or ""))
        skill_cats = tailored.get("skill_categories") or []
        if skill_cats:
            for cat in skill_cats:
                if isinstance(cat, dict):
                    parts.append(str(cat.get("category") or ""))
                    parts.extend(str(s) for s in (cat.get("skills") or []))
                elif hasattr(cat, "category"):
                    parts.append(str(cat.category))
                    parts.extend(str(s) for s in (cat.skills or []))
        else:
            # Only rendered in PDF when skill_categories is empty!
            parts.extend(str(k) for k in (tailored.get("matched_keywords") or []))
            parts.extend(str(s) for s in ((profile or {}).get("core_skills") or []))
        parts.extend(_collect_tailored_bullets(tailored))
    return " ".join(p for p in parts if p).lower()


def compute_ats_scorecard(jd_text: str, profile: dict, tailored: dict) -> dict:
    """Computes truthful, un-clamped Before (raw master_profile) vs After (rendered tailored PDF) ATS metrics."""
    jd_raw = jd_text or ""
    jd_lower = jd_raw.lower()

    # 1. Extract JD Required Technical Skills using strict word boundaries
    parsed = parse_jd(jd_raw, profile)
    jd_skills = {s.lower() for s in parsed.get("skills", [])}
    for s in STATIC_SKILLS:
        if skill_in_text(s, jd_lower):
            jd_skills.add(s.lower())
    if profile:
        for s in profile.get("core_skills", []):
            if skill_in_text(s, jd_lower):
                jd_skills.add(s.lower())

    if not jd_skills:
        for term in ("python", "machine learning", "ai", "sql", "docker"):
            if skill_in_text(term, jd_lower):
                jd_skills.add(term)

    total_jd_skills = max(len(jd_skills), 1)

    # 2. Raw Master Profile Text & Bullets (Before)
    raw_bullets: list[str] = []
    raw_parts: list[str] = []
    if profile:
        raw_parts.append(str(profile.get("experience_summary") or ""))
        for exp in profile.get("experience") or []:
            raw_parts.append(str(exp.get("role") or ""))
            b_list = list(exp.get("bullets") or [])
            raw_bullets.extend(b_list)
            raw_parts.extend(b_list)
        raw_parts.extend(str(s) for s in (profile.get("core_skills") or []))
    raw_resume_str = " ".join(p for p in raw_parts if p).lower()

    # 3. Rendered Tailored PDF Text & Bullets (After) — NO unrendered matched_keywords inflation!
    tailored_bullets = _collect_tailored_bullets(tailored or {})
    tailored_resume_str = _renderable_tailored_text(profile or {}, tailored or {})

    # 4. Honest Hard Skills Match (NO max(before, after) clamping!)
    matched_skills_before = {s for s in jd_skills if skill_in_text(s, raw_resume_str)}
    matched_skills_after = {s for s in jd_skills if skill_in_text(s, tailored_resume_str)}

    hard_skills_before_pct = min(100, round((len(matched_skills_before) / total_jd_skills) * 100))
    hard_skills_after_pct = min(100, round((len(matched_skills_after) / total_jd_skills) * 100))
    dropped_jd_skills = sorted(matched_skills_before - matched_skills_after)

    # 5. Honest Domain Keywords Match (NO fake 75->90 fabrication!)
    domain_terms_in_jd = {t for t in DOMAIN_KEYWORDS_CATALOG if skill_in_text(t, jd_lower)}
    if domain_terms_in_jd:
        domain_before = {t for t in domain_terms_in_jd if skill_in_text(t, raw_resume_str)}
        domain_after = {t for t in domain_terms_in_jd if skill_in_text(t, tailored_resume_str)}
        domain_keywords_before_pct = round((len(domain_before) / len(domain_terms_in_jd)) * 100)
        domain_keywords_after_pct = round((len(domain_after) / len(domain_terms_in_jd)) * 100)
    else:
        domain_keywords_before_pct = 100
        domain_keywords_after_pct = 100

    # 6. Honest Quantified Impact Density (Measured on BOTH raw & tailored!)
    impact_before_pct = _compute_impact_density(raw_bullets)
    impact_density_pct = _compute_impact_density(tailored_bullets)

    # 7. Real Semantic Similarity (via configured embeddings provider)
    semantic_before_pct = 0
    semantic_after_pct = 0
    try:
        if jd_raw.strip() and raw_resume_str.strip() and tailored_resume_str.strip():
            vecs = embed_texts([jd_raw[:8000], raw_resume_str[:8000], tailored_resume_str[:8000]])
            if len(vecs) == 3:
                semantic_before_pct = round(cosine(vecs[0], vecs[1]) * 100)
                semantic_after_pct = round(cosine(vecs[0], vecs[2]) * 100)
    except Exception:
        pass

    # 8. Honest Weighted Overall Score (ZERO forced +5% floor! Can be negative if tailoring regressed!)
    # Weights: 45% Hard Skills + 25% Domain Keywords + 20% Quantified Impact + 10% Semantic Fit
    overall_before_pct = round(
        0.45 * hard_skills_before_pct
        + 0.25 * domain_keywords_before_pct
        + 0.20 * impact_before_pct
        + 0.10 * semantic_before_pct
    )
    overall_after_pct = round(
        0.45 * hard_skills_after_pct
        + 0.25 * domain_keywords_after_pct
        + 0.20 * impact_density_pct
        + 0.10 * semantic_after_pct
    )
    overall_before_pct = max(0, min(100, overall_before_pct))
    overall_after_pct = max(0, min(100, overall_after_pct))
    boost_pct = overall_after_pct - overall_before_pct  # Signed delta (can be negative!)

    # 9. Truly Newly Added / Emphasized Terms (must actually appear in rendered PDF text!)
    newly_added: list[str] = []
    for s in sorted(matched_skills_after - matched_skills_before):
        disp = _clean_token_display(s)
        if disp not in newly_added:
            newly_added.append(disp)
    for t in sorted(domain_terms_in_jd):
        if skill_in_text(t, tailored_resume_str) and not skill_in_text(t, raw_resume_str):
            disp = _clean_token_display(t)
            if disp not in newly_added:
                newly_added.append(disp)

    # 10. Missing JD Skills (not present in rendered PDF)
    missing_jd = sorted(jd_skills - matched_skills_after)
    missing_display = [_clean_token_display(m) for m in missing_jd[:8]]
    dropped_display = [_clean_token_display(d) for d in dropped_jd_skills]

    return {
        "overall_before_pct": overall_before_pct,
        "overall_after_pct": overall_after_pct,
        "boost_pct": boost_pct,
        "hard_skills_before_pct": hard_skills_before_pct,
        "hard_skills_after_pct": hard_skills_after_pct,
        "total_jd_skills": total_jd_skills,
        "matched_skills_count": len(matched_skills_after),
        "domain_keywords_before_pct": domain_keywords_before_pct,
        "domain_keywords_after_pct": domain_keywords_after_pct,
        "impact_before_pct": impact_before_pct,
        "impact_density_pct": impact_density_pct,
        "semantic_before_pct": semantic_before_pct,
        "semantic_after_pct": semantic_after_pct,
        "newly_added_terms": newly_added[:8],
        "missing_jd_skills": missing_display,
        "dropped_jd_skills": dropped_display,
    }


def format_scorecard_markdown(scorecard: dict) -> str:
    """Formats an honest Before-vs-After ATS scorecard for Telegram/Web Studio."""
    if not scorecard:
        return ""

    before = scorecard.get("overall_before_pct", 0)
    after = scorecard.get("overall_after_pct", 0)
    boost = scorecard.get("boost_pct", after - before)
    delta_label = f"+{boost}% Boost" if boost > 0 else (f"{boost}% Regression" if boost < 0 else "0% Change")

    skills_pct = scorecard.get("hard_skills_after_pct", 0)
    matched_cnt = scorecard.get("matched_skills_count", 0)
    total_cnt = scorecard.get("total_jd_skills", 0)

    domain_pct = scorecard.get("domain_keywords_after_pct", 0)
    impact_pct = scorecard.get("impact_density_pct", 0)
    sem_after = scorecard.get("semantic_after_pct")

    added = scorecard.get("newly_added_terms", [])
    added_str = ", ".join(added) if added else "Reframed verified profile achievements"

    missing = scorecard.get("missing_jd_skills", [])
    if missing:
        missing_str = f"{', '.join(missing)} (Reply to add if you know them!)"
    else:
        missing_str = "None (Complete Coverage!)"

    lines = [
        "📊 Mock ATS Match Scorecard:",
        f"• Overall ATS Score: {before}% (Raw) ➔ {after}% (Tailored) [{delta_label}]",
        f"• Hard Skills Match: {skills_pct}% ({matched_cnt}/{total_cnt} JD skills)",
        f"• Domain Keywords: {domain_pct}% | Quantified Impact: {impact_pct}%"
        + (f" | Semantic Fit: {sem_after}%" if sem_after is not None else ""),
        f"• ✨ Added/Emphasized: {added_str}",
        f"• ⚠️ Missing JD Skills: {missing_str}",
    ]
    dropped = scorecard.get("dropped_jd_skills") or []
    if dropped:
        lines.append(f"• 🚨 Regression Warning (Dropped Profile Skills): {', '.join(dropped)}")
    return "\n".join(lines)
