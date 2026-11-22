from __future__ import annotations

from collections.abc import Callable
import logging
import re

from bs4 import BeautifulSoup

from src.config import AppConfig, load_config
from src.db import get_connection, make_dedup_hash
from src.dedupe import is_near_dup, normalize_title
from src.ingestors.location_policy import (
    evaluate_job_location,
    normalize_location_text,
)

logger = logging.getLogger(__name__)

_JOB_INSERTED_HOOK: Callable[[dict], None] | None = None


def set_job_inserted_hook(hook: Callable[[dict], None] | None) -> None:
    global _JOB_INSERTED_HOOK
    _JOB_INSERTED_HOOK = hook


def _cfg() -> AppConfig:
    return load_config()


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)


def tokenize(text: str):
    return re.findall(r"[a-z0-9+#\.]+", (text or "").lower())


def norm_loc(loc: str) -> str:
    return normalize_location_text(loc)


def extract_min_required_years(title: str = "", jd_text: str = "") -> int | None:
    """Extracts minimum years of experience explicitly required (e.g. '6 to 10 years' -> 6, '8+ years' -> 8)."""
    combined = f"{title or ''}\n{(jd_text or '')[:3500]}"
    mins: list[int] = []
    # Range patterns like '6 to 10 years', '6-10 yrs', '7 – 12 years'
    for m in re.finditer(r"\b(\d{1,2})\s*(?:to|-|–|—)\s*(\d{1,2})\s*(?:\+?\s*)?(?:years?|yrs?)\b", combined, re.IGNORECASE):
        lo = int(m.group(1))
        hi = int(m.group(2))
        if 0 < lo <= hi <= 25:
            mins.append(lo)
    # Single lower-bound patterns like '8+ years of experience', 'minimum 7 years'
    for m in re.finditer(
        r"(?:minimum\s+(?:of\s+)?|at\s+least\s+)?(\d{1,2})\s*\+\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|relevant|industry|engineering|professional)",
        combined,
        re.IGNORECASE,
    ):
        val = int(m.group(1))
        if 0 < val <= 25:
            mins.append(val)
    if not mins:
        return None
    # Use the smallest explicit minimum requirement if multiple appear (conservative: avoids false rejections)
    return min(mins)


def matches_basic_filters(
    title: str,
    location: str,
    jd_text: str = "",
    is_remote: bool | None = None,
    cfg: AppConfig | None = None,
) -> bool:
    active_cfg = cfg or _cfg()
    prefs = active_cfg.candidate_preferences
    t_low = (title or "").lower()

    # 1. Exclusion: word-boundary title filter (blocks intern, lead, staff, director, federal, etc.)
    for bad in prefs.exclude_title_keywords:
        if re.search(r"\b" + re.escape(bad.lower()) + r"\b", t_low):
            return False

    # 2. Target role keyword match
    tech_tokens = {"c++", "c#", ".net", "node.js", "ai", "ml"}
    matched = False
    for good in prefs.target_role_keywords:
        g = good.lower()
        if g in tech_tokens:
            if re.search(r"(?:^|[^a-z0-9])" + re.escape(g) + r"(?:$|[^a-z0-9])", t_low):
                matched = True
                break
        else:
            if re.search(r"\b" + re.escape(g) + r"\b", t_low):
                matched = True
                break
    if not matched:
        return False

    # 3. Optional max_experience_years check against explicit experience bounds
    if prefs.max_experience_years is not None:
        min_req = extract_min_required_years(title, jd_text)
        if min_req is not None and min_req > prefs.max_experience_years:
            return False

    # 4. Strict composable LocationPolicy evaluation (OR / AND / XOR / NOT)
    policy = active_cfg.effective_location_policy()
    loc_ok, _reason, _facts = evaluate_job_location(
        title=title,
        location=location,
        jd_text=jd_text,
        is_remote_hint=is_remote,
        policy=policy,
    )
    return loc_ok


def job_exists(conn, dedup_hash: str) -> bool:
    if conn is None:
        try:
            from src.ingestors.dedup_cache import CACHE

            if CACHE.hydrated:
                if CACHE.has_hash(dedup_hash):
                    return True
        except Exception:
            pass

        try:
            with get_connection() as c, c.cursor() as cur:
                cur.execute("SELECT 1 FROM jobs WHERE dedup_hash = %s", (dedup_hash,))
                return cur.fetchone() is not None
        except Exception:
            return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE dedup_hash = %s", (dedup_hash,))
        return cur.fetchone() is not None


def insert_job(
    conn,
    source_ats: str,
    company: str,
    title: str,
    location: str,
    apply_url: str,
    jd_text: str,
    original_source_url: str | None = None,
    work_mode: str | None = None,
    external_job_id: str | None = None,
    hiring_contact_email: str | None = None,
) -> bool:
    dedup_hash = make_dedup_hash(company, title, location)

    if conn is None:
        try:
            from src.ingestors.dedup_cache import CACHE

            if CACHE.hydrated:
                if CACHE.has_hash(dedup_hash):
                    return False
                norm = normalize_title(title)
                if norm:
                    sorted_norm = " ".join(sorted(norm.split()))
                    if CACHE.is_near_dup_cached(company, sorted_norm):
                        return False
        except Exception:
            pass

        try:
            with get_connection() as c:
                return insert_job(
                    c,
                    source_ats,
                    company,
                    title,
                    location,
                    apply_url,
                    jd_text,
                    original_source_url=original_source_url,
                    work_mode=work_mode,
                    external_job_id=external_job_id,
                    hiring_contact_email=hiring_contact_email,
                )
        except Exception as e:
            logger.warning("Could not insert job: %s", e)
            return False

    if is_near_dup(conn, company, title, work_mode=work_mode):
        return False
    canonical = normalize_title(title)
    with conn.cursor() as cur:
        # Tier 1 external_job_id dedupe check if provided
        if external_job_id:
            try:
                cur.execute(
                    "SELECT 1 FROM jobs WHERE source_ats = %s AND external_job_id = %s;",
                    (source_ats, external_job_id),
                )
                if cur.fetchone() is not None:
                    return False
            except Exception:
                if hasattr(conn, "rollback"):
                    conn.rollback()

        cur.execute(
            """
            INSERT INTO jobs (
                dedup_hash, source_ats, company, title, location, apply_url, jd_text,
                canonical_title, original_source_url, work_mode, external_job_id, hiring_contact_email
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dedup_hash) DO NOTHING
            RETURNING id;
            """,
            (
                dedup_hash,
                source_ats,
                company,
                title,
                location,
                apply_url,
                jd_text,
                canonical,
                original_source_url,
                work_mode,
                external_job_id,
                hiring_contact_email,
            ),
        )
        row = cur.fetchone()
        inserted_id = row["id"] if isinstance(row, dict) else (row[0] if row else None)

    conn.commit()

    if inserted_id is not None:
        try:
            from src.ingestors.dedup_cache import CACHE

            CACHE.record_job(dedup_hash, company, canonical)
        except Exception:
            pass

        if _JOB_INSERTED_HOOK is not None:
            job_dict = {
                "id": inserted_id,
                "dedup_hash": dedup_hash,
                "source_ats": source_ats,
                "company": company,
                "title": title,
                "location": location,
                "apply_url": apply_url,
                "jd_text": jd_text,
                "canonical_title": canonical,
                "original_source_url": original_source_url,
                "work_mode": work_mode,
                "external_job_id": external_job_id,
                "hiring_contact_email": hiring_contact_email,
            }
            try:
                _JOB_INSERTED_HOOK(job_dict)
            except Exception as e:
                logger.warning("Error in _JOB_INSERTED_HOOK: %s", e)
        return True
    return False
