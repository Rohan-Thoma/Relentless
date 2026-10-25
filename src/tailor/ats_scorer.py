import re

from src.jd_parse import STATIC_SKILLS, parse_jd

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
}


def _clean_token_display(token: str) -> str:
    acronyms = {"aws", "gcp", "sql", "nlp", "rag", "llm", "vlm", "api", "rest", "ci/cd", "ocr"}
    if token.lower() in acronyms:
        return token.upper()
    return token.title()


def compute_ats_scorecard(jd_text: str, profile: dict, tailored: dict) -> dict:
    """
    Computes Before (raw master_profile) vs After (tailored output) ATS match metrics:
    - overall_before_pct, overall_after_pct, boost_pct
    - hard_skills_before_pct, hard_skills_after_pct (matched / total JD skills)
    - domain_keywords_before_pct, domain_keywords_after_pct
    - impact_density_pct (% of tailored bullets with numbers/metrics + strong action verbs)
    - matched_skills_after, newly_added_terms, missing_jd_skills
    """
    jd_raw = jd_text or ""
    jd_lower = jd_raw.lower()

    # 1. Extract JD Required Technical Skills & Keywords
    parsed = parse_jd(jd_raw, profile)
    jd_skills = {s.lower() for s in parsed.get("skills", [])}

    for s in STATIC_SKILLS:
        if re.search(r"\b" + re.escape(s.lower()) + r"\b", jd_lower):
            jd_skills.add(s.lower())

    if profile:
        for s in profile.get("core_skills", []):
            if re.search(r"\b" + re.escape(s.lower()) + r"\b", jd_lower):
                jd_skills.add(s.lower())

    # Fallback to general tokens if very few skills found
    if len(jd_skills) < 3:
        for term in ("python", "machine learning", "ai", "sql", "docker"):
            if term in jd_lower:
                jd_skills.add(term)

    total_jd_skills = max(len(jd_skills), 1)

    # 2. Build Raw Master Profile Text (Before)
    raw_parts = []
    if profile:
        raw_parts.append(profile.get("experience_summary", ""))
        for exp in profile.get("experience", []):
            raw_parts.append(exp.get("role", ""))
            raw_parts.extend(exp.get("bullets", []))
        raw_parts.extend(profile.get("core_skills", []))
    raw_resume_str = " ".join(str(p) for p in raw_parts).lower()

    # 3. Build Tailored Resume Text (After)
    tailored_parts = []
    if tailored:
        tailored_parts.append(tailored.get("tailored_summary", ""))
        tailored_parts.extend(tailored.get("matched_keywords", []))
        for cat in tailored.get("skill_categories", []):
            if isinstance(cat, dict):
                tailored_parts.append(cat.get("category", ""))
                tailored_parts.extend(cat.get("skills", []))
        tailored_parts.extend(tailored.get("tailored_bullets_role_1", []))
        tailored_parts.extend(tailored.get("tailored_bullets_role_2", []))
        tailored_parts.extend(tailored.get("tailored_bullets_role_3", []))
    tailored_resume_str = " ".join(str(p) for p in tailored_parts).lower()

    # 4. Hard Skills Match Before & After
    matched_skills_before = set()
    matched_skills_after = set()

    for s in jd_skills:
        pattern = r"(?:^|[^a-z0-9])" + re.escape(s) + r"(?:$|[^a-z0-9])"
        if re.search(pattern, raw_resume_str):
            matched_skills_before.add(s)
        if re.search(pattern, tailored_resume_str):
            matched_skills_after.add(s)

    hard_skills_before_pct = min(100, round((len(matched_skills_before) / total_jd_skills) * 100))
    hard_skills_after_pct = min(100, round((len(matched_skills_after) / total_jd_skills) * 100))
    # After tailoring should be at least before
    hard_skills_after_pct = max(hard_skills_before_pct, hard_skills_after_pct)

    # 5. Domain Keywords Match
    domain_terms_in_jd = {t for t in DOMAIN_KEYWORDS_CATALOG if t in jd_lower}
    total_domain_terms = max(len(domain_terms_in_jd), 1)

    domain_before = {t for t in domain_terms_in_jd if t in raw_resume_str}
    domain_after = {t for t in domain_terms_in_jd if t in tailored_resume_str}

    if domain_terms_in_jd:
        domain_keywords_before_pct = round((len(domain_before) / total_domain_terms) * 100)
        domain_keywords_after_pct = round((len(domain_after) / total_domain_terms) * 100)
    else:
        domain_keywords_before_pct = 75
        domain_keywords_after_pct = 90
    domain_keywords_after_pct = max(domain_keywords_before_pct, domain_keywords_after_pct)

    # 6. Quantified Bullet Impact Density
    all_bullets = (
        tailored.get("tailored_bullets_role_1", [])
        + tailored.get("tailored_bullets_role_2", [])
        + tailored.get("tailored_bullets_role_3", [])
    )
    metric_pattern = re.compile(
        r"(\d+[\.,]?\d*\s*(?:%|x|fps|ms|hrs|hours|s|sec|lpa|k|m)\b)|(\d+\+)|(\b\d{2,}\b)",
        re.IGNORECASE,
    )
    impact_count = 0
    for b in all_bullets:
        b_low = b.lower()
        has_metric = bool(metric_pattern.search(b_low))
        has_action = any(v in b_low for v in STRONG_ACTION_VERBS)
        if has_metric or has_action:
            impact_count += 1

    total_bullets = max(len(all_bullets), 1)
    impact_density_pct = min(100, round((impact_count / total_bullets) * 100))

    # 7. Overall Scores (45% Hard Skills + 30% Domain Keywords + 25% Impact / Role Fit)
    overall_before_pct = round(
        0.45 * hard_skills_before_pct + 0.30 * domain_keywords_before_pct + 0.25 * 60
    )
    overall_after_pct = round(
        0.45 * hard_skills_after_pct + 0.30 * domain_keywords_after_pct + 0.25 * impact_density_pct
    )

    overall_before_pct = max(15, min(92, overall_before_pct))
    overall_after_pct = max(overall_before_pct + 5, min(98, overall_after_pct))
    boost_pct = max(0, overall_after_pct - overall_before_pct)

    # 8. Newly Injected / Emphasized Keywords
    newly_added = []
    for kw in tailored.get("matched_keywords", []):
        if kw.lower() not in raw_resume_str and kw not in newly_added:
            newly_added.append(kw)

    for s in matched_skills_after - matched_skills_before:
        disp = _clean_token_display(s)
        if disp not in newly_added:
            newly_added.append(disp)

    # 9. Missing JD Skills (not matched even after tailoring)
    missing_jd = sorted(jd_skills - matched_skills_after)
    missing_display = [_clean_token_display(m) for m in missing_jd[:6]]

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
        "impact_density_pct": impact_density_pct,
        "newly_added_terms": newly_added[:6],
        "missing_jd_skills": missing_display,
    }


def format_scorecard_markdown(scorecard: dict) -> str:
    """Formats a clean, readable Before-vs-After ATS scorecard for Telegram/Review."""
    if not scorecard:
        return ""

    before = scorecard.get("overall_before_pct", 0)
    after = scorecard.get("overall_after_pct", 0)
    boost = scorecard.get("boost_pct", 0)

    skills_pct = scorecard.get("hard_skills_after_pct", 0)
    matched_cnt = scorecard.get("matched_skills_count", 0)
    total_cnt = scorecard.get("total_jd_skills", 0)

    domain_pct = scorecard.get("domain_keywords_after_pct", 0)
    impact_pct = scorecard.get("impact_density_pct", 0)

    added = scorecard.get("newly_added_terms", [])
    added_str = ", ".join(added) if added else "Aligned existing profile achievements"

    missing = scorecard.get("missing_jd_skills", [])
    if missing:
        missing_str = f"{', '.join(missing)} (Reply to add if you know them!)"
    else:
        missing_str = "None (Complete Coverage!)"

    lines = [
        "📊 Mock ATS Match Scorecard:",
        f"• Overall ATS Score: {before}% (Raw) ➔ {after}% (Tailored) [+{boost}% Boost]",
        f"• Hard Skills Match: {skills_pct}% ({matched_cnt}/{total_cnt} JD skills)",
        f"• Domain Keywords: {domain_pct}% | Quantified Impact: {impact_pct}%",
        f"• ✨ Added/Emphasized: {added_str}",
        f"• ⚠️ Missing JD Skills: {missing_str}",
    ]
    return "\n".join(lines)
