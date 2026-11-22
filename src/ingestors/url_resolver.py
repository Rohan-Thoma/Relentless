from __future__ import annotations

from difflib import SequenceMatcher
import json
import logging
import os
import re
import urllib.parse

from bs4 import BeautifulSoup
import httpx

from src.config import load_config
from src.db import get_connection

logger = logging.getLogger(__name__)

DIRECT_ATS_DOMAINS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "recruitee.com",
    "bamboohr.com",
    "icims.com",
    "workable.com",
)

IGNORED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "linkedin.com",
    "naukri.com",
    "indeed.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "sentry.io",
    "w3.org",
}


def extract_public_hiring_email(text: str) -> str | None:
    """Extracts an explicitly listed public recruiting/careers email from JD or careers text."""
    if not text:
        return None
    candidates = re.findall(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", text)
    preferred: list[str] = []
    fallback: list[str] = []
    for email_addr in candidates:
        low = email_addr.lower()
        local, _, domain = low.partition("@")
        if not domain or domain in IGNORED_EMAIL_DOMAINS:
            continue
        if any(bad in local for bad in ("noreply", "no-reply", "donotreply", "privacy", "legal", "dpo", "support", "unsubscribe", "accessibility")):
            continue
        if any(good in local for good in ("career", "job", "recruit", "talent", "hiring", "people", "hr", "founder", "eng")):
            if low not in preferred:
                preferred.append(low)
        elif low not in fallback:
            fallback.append(low)
    return preferred[0] if preferred else (fallback[0] if fallback else None)


def extract_ats_board_from_url(url: str) -> dict | None:
    if not url:
        return None

    # 1. Workday
    wd_match = re.search(
        r"https?://([a-zA-Z0-9_\-]+)\.([a-zA-Z0-9_\-]+)\.myworkdayjobs\.com/(.+)",
        url,
    )
    if wd_match:
        tenant = wd_match.group(1)
        wd_instance = wd_match.group(2)
        rest = wd_match.group(3)

        site = None
        cxs_match = re.match(r"wday/cxs/[^/]+/([^/?#]+)", rest)
        if cxs_match:
            site = cxs_match.group(1)
        else:
            parts = [p for p in rest.split("/") if p]
            if parts:
                if re.match(r"^[a-zA-Z]{2}(?:-[a-zA-Z]{2,4})?$", parts[0]) and len(parts) > 1:
                    site = parts[1]
                else:
                    site = parts[0]

        if site:
            company = tenant.capitalize()
            return {
                "platform": "workday_cxs",
                "slug": f"{tenant}:{wd_instance}:{site}",
                "company": company,
                "metadata": {
                    "company": company,
                    "tenant": tenant,
                    "wd_instance": wd_instance,
                    "site": site,
                },
            }

    # 2. Greenhouse
    gh_match = re.search(
        r"https?://(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)",
        url,
    )
    if gh_match:
        slug = gh_match.group(1)
        if slug not in ("embed", "v1"):
            return {
                "platform": "greenhouse",
                "slug": slug,
                "company": slug.capitalize(),
            }

    # 3. Lever
    lever_match = re.search(r"https?://jobs\.lever\.co/([^/?#]+)", url)
    if lever_match:
        slug = lever_match.group(1)
        return {
            "platform": "lever",
            "slug": slug,
            "company": slug.capitalize(),
        }

    # 4. Ashby
    ashby_match = re.search(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", url)
    if ashby_match:
        slug = ashby_match.group(1)
        return {
            "platform": "ashby",
            "slug": slug,
            "company": slug.capitalize(),
        }

    # 5. SmartRecruiters
    sr_match = re.search(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", url)
    if sr_match:
        slug = sr_match.group(1)
        return {
            "platform": "smartrecruiters",
            "slug": slug,
            "company": slug,
        }

    return None


def register_discovered_board(url: str, conn=None) -> bool:
    info = extract_ats_board_from_url(url)
    if not info:
        return False

    platform = info["platform"]
    slug = info["slug"]

    try:
        from src.ingestors.dedup_cache import CACHE

        if CACHE.hydrated and not os.getenv("PYTEST_CURRENT_TEST"):
            if CACHE.has_board(platform, slug):
                return False
    except Exception:
        pass

    def _execute(c):
        with c.cursor() as cur:
            metadata_json = json.dumps(info.get("metadata", {}))
            cur.execute(
                """
                INSERT INTO discovered_ats_boards (platform, slug, company, metadata)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (platform, slug) DO NOTHING
                RETURNING id;
                """,
                (platform, slug, info.get("company"), metadata_json),
            )
            inserted = cur.fetchone() is not None
            c.commit()
            if inserted:
                try:
                    from src.ingestors.dedup_cache import CACHE

                    CACHE.record_board(platform, slug)
                except Exception:
                    pass
            return inserted

    if conn is not None:
        try:
            return _execute(conn)
        except Exception as e:
            logger.warning("Failed to register discovered board for %s: %s", url, e)
            return False

    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return False

    try:
        with get_connection() as c:
            return _execute(c)
    except Exception as e:
        logger.warning("Failed to register discovered board for %s: %s", url, e)
        return False


def _clean_ddg_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if "duckduckgo.com/l/?uddg=" in raw_url:
        parsed = urllib.parse.urlparse(raw_url)
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("uddg"):
            return params["uddg"][0]
    return raw_url


def _normalize_company_slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (company or "").lower())


def _lookup_known_ats_board(company: str) -> tuple[str, str] | None:
    """Matches a company name against configured ATS slugs (zero web scraping)."""
    norm = _normalize_company_slug(company)
    if not norm or len(norm) < 3:
        return None
    try:
        ats = load_config().ats_targets
    except Exception:
        return None

    for plat, slugs in (
        ("greenhouse", ats.greenhouse),
        ("lever", ats.lever),
        ("ashby", ats.ashby),
    ):
        for slug in slugs:
            if _normalize_company_slug(slug) == norm:
                return plat, slug
    return None


async def resolve_via_ats_registry(
    client: httpx.AsyncClient, company: str, title: str
) -> str | None:
    """Stage 2 Career Resolver: queries known Greenhouse/Lever/Ashby JSON API directly."""
    found = _lookup_known_ats_board(company)
    if not found:
        return None
    plat, slug = found
    t_low = (title or "").strip().lower()
    if not t_low:
        return None

    try:
        candidates: list[tuple[str, str]] = []
        if plat == "greenhouse":
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=8.0)
            if r.status_code == 200:
                candidates = [
                    (str(j.get("title", "")), str(j.get("absolute_url", "")))
                    for j in r.json().get("jobs", [])
                ]
        elif plat == "lever":
            r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=8.0)
            if r.status_code == 200 and isinstance(r.json(), list):
                candidates = [
                    (str(j.get("text", "")), str(j.get("hostedUrl", "")))
                    for j in r.json()
                ]
        elif plat == "ashby":
            r = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=8.0)
            if r.status_code == 200:
                candidates = [
                    (str(j.get("title", "")), str(j.get("jobUrl", "")))
                    for j in r.json().get("jobs", [])
                ]

        best_url = None
        best_score = 0.0
        for cand_title, cand_url in candidates:
            if not cand_url:
                continue
            score = SequenceMatcher(None, t_low, cand_title.strip().lower()).ratio()
            if score > best_score:
                best_score = score
                best_url = cand_url
        if best_url and best_score >= 0.72:
            register_discovered_board(best_url)
            return best_url
    except Exception as e:
        logger.debug("ATS registry resolution failed for %s (%s/%s): %s", company, plat, slug, e)
    return None


async def resolve_direct_apply_url(
    client: httpx.AsyncClient, company: str, title: str, fallback_url: str
) -> str:
    """3-stage Career-Site Resolver:
    1. Already a direct ATS URL -> return immediately
    2. Direct ATS Board Registry JSON lookup (zero scraping, zero login walls)
    3. Optional Search API (Tavily/Serper) or DuckDuckGo HTML fallback
    """
    url_low = (fallback_url or "").lower()
    if any(domain in url_low for domain in DIRECT_ATS_DOMAINS):
        register_discovered_board(fallback_url)
        return fallback_url

    # Stage 2: Direct ATS Board Registry (fast JSON API)
    registry_url = await resolve_via_ats_registry(client, company, title)
    if registry_url:
        return registry_url

    ignored_domains = ("linkedin.com", "naukri.com", "indeed.com", "duckduckgo.com", "glassdoor.com")
    search_query = (
        f'"{company}" "{title}" (site:greenhouse.io OR site:lever.co OR '
        f'site:ashbyhq.com OR site:myworkdayjobs.com OR site:smartrecruiters.com OR apply careers)'
    )

    # Stage 3a: Optional Tavily Search API if configured
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            t_resp = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": tavily_key, "query": search_query, "max_results": 5},
                timeout=10.0,
            )
            if t_resp.status_code == 200:
                for item in t_resp.json().get("results", []):
                    u = str(item.get("url") or "")
                    if u.startswith("http") and not any(ign in u.lower() for ign in ignored_domains):
                        register_discovered_board(u)
                        return u
        except Exception as e:
            logger.debug("Tavily career resolution failed: %s", e)

    # Stage 3b: DuckDuckGo HTML fallback
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": search_query},
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.select("a.result__url, a.result__a, a.result__snippet"):
                href = link.get("href")
                clean_href = _clean_ddg_url(href)
                if not clean_href or not clean_href.startswith("http"):
                    continue
                href_low = clean_href.lower()
                if any(ign in href_low for ign in ignored_domains):
                    continue
                register_discovered_board(clean_href)
                return clean_href
    except Exception as e:
        logger.debug("Failed to resolve direct apply URL via DuckDuckGo: %s", e)

    return fallback_url
