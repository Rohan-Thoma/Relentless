from collections.abc import Callable
import logging
import re

from bs4 import BeautifulSoup

from src.config import load_config
from src.db import get_connection, make_dedup_hash
from src.dedupe import is_near_dup, normalize_title

logger = logging.getLogger(__name__)

_JOB_INSERTED_HOOK: Callable[[dict], None] | None = None


def set_job_inserted_hook(hook: Callable[[dict], None] | None) -> None:
    global _JOB_INSERTED_HOOK
    _JOB_INSERTED_HOOK = hook


def _cfg():
    return load_config()


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)


def tokenize(text: str):
    return re.findall(r"[a-z0-9+#\.]+", (text or "").lower())


def norm_loc(loc: str) -> str:
    l = (loc or "").lower().strip()
    word_aliases = {
        r"\bbengaluru\b": "bangalore",
        r"\bgurugram\b": "gurgaon",
        r"\bnew delhi\b": "delhi",
        r"\bncr\b": "delhi",
        r"\bsecunderabad\b": "hyderabad",
        r"\bnavi mumbai\b": "mumbai",
        r"\bkarnataka\b": "bangalore india",
        r"\btelangana\b": "hyderabad india",
        r"\bmaharashtra\b": "mumbai pune india",
        r"\btamil nadu\b": "chennai india",
        r"\bup\b": "noida india",
        r",\s*in\b": ", india",
        r"\bin\s*$": "india",
    }
    for pat, repl in word_aliases.items():
        l = re.sub(pat, repl, l)
    return l


def matches_basic_filters(title: str, location: str) -> bool:
    cfg = _cfg()
    prefs = cfg.candidate_preferences
    t_low = (title or "").lower()
    l_low = norm_loc(location or "")

    # exclusion: word-boundary
    for bad in prefs.exclude_title_keywords:
        if re.search(r"\b" + re.escape(bad.lower()) + r"\b", t_low):
            return False

    # keyword match: technical tokens substring or word-boundary; plain words word-boundary
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

    # US/EU/UK/Canada only rejection (unless "india" explicitly mentioned)
    us_eu_only = re.search(
        r"\b(us only|usa only|u\.s\. only|united states only|eu only|uk only|canada only|must reside in the u\.s)\b",
        l_low,
    )
    if us_eu_only and "india" not in l_low:
        return False

    # location match
    allowed_locs_lower = [loc.lower() for loc in prefs.allowed_locations]
    remote_keywords = {
        "remote",
        "hybrid",
        "anywhere",
        "worldwide",
        "global",
        "work from home",
        "wfh",
        "distributed",
        "apac",
    }
    has_remote = any(re.search(r"\b" + re.escape(rw) + r"\b", l_low) for rw in remote_keywords)

    loc_ok = False
    if (
        (
            "remote" in allowed_locs_lower
            or "anywhere" in allowed_locs_lower
            or "global" in allowed_locs_lower
            or "worldwide" in allowed_locs_lower
        )
        and has_remote
    ) or ("india" in allowed_locs_lower and "india" in l_low):
        loc_ok = True
    else:
        for loc in allowed_locs_lower:
            if re.search(r"\b" + re.escape(loc) + r"\b", l_low):
                loc_ok = True
                break
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
                    original_source_url,
                )
        except Exception as e:
            logger.warning("Could not insert job: %s", e)
            return False

    if is_near_dup(conn, company, title):
        return False
    canonical = normalize_title(title)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (dedup_hash, source_ats, company, title, location, apply_url, jd_text, canonical_title, original_source_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            }
            try:
                _JOB_INSERTED_HOOK(job_dict)
            except Exception as e:
                logger.warning("Error in _JOB_INSERTED_HOOK: %s", e)
        return True
    return False
