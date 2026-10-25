import json
import logging
import os
import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup

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


def extract_ats_board_from_url(url: str) -> dict | None:
    if not url:
        return None

    # 1. Workday
    # Matches https://{tenant}.{wd_instance}.myworkdayjobs.com/(?:[a-zA-Z]{2}(?:-[a-zA-Z]{2,4})?/)?([^/?#]+)
    # Also handles /wday/cxs/{tenant}/{site}
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
                # check if first segment is a locale code like en-US, en, fr-FR
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
    # Matches https://(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)
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
    # Matches https://jobs\.lever\.co/([^/?#]+)
    lever_match = re.search(r"https?://jobs\.lever\.co/([^/?#]+)", url)
    if lever_match:
        slug = lever_match.group(1)
        return {
            "platform": "lever",
            "slug": slug,
            "company": slug.capitalize(),
        }

    # 4. Ashby
    # Matches https://jobs\.ashbyhq\.com/([^/?#]+)
    ashby_match = re.search(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", url)
    if ashby_match:
        slug = ashby_match.group(1)
        return {
            "platform": "ashby",
            "slug": slug,
            "company": slug.capitalize(),
        }

    # 5. SmartRecruiters
    # Matches https://jobs\.smartrecruiters\.com/([^/?#]+)
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
                (info["platform"], info["slug"], info.get("company"), metadata_json),
            )
            inserted = cur.fetchone() is not None
            c.commit()
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
    # DuckDuckGo HTML results may wrap urls in //duckduckgo.com/l/?uddg=URL
    if "duckduckgo.com/l/?uddg=" in raw_url:
        parsed = urllib.parse.urlparse(raw_url)
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("uddg"):
            return params["uddg"][0]
    return raw_url


async def resolve_direct_apply_url(
    client: httpx.AsyncClient, company: str, title: str, fallback_url: str
) -> str:
    url_low = (fallback_url or "").lower()
    if any(domain in url_low for domain in DIRECT_ATS_DOMAINS):
        register_discovered_board(fallback_url)
        return fallback_url

    # Query DuckDuckGo HTML for direct company ATS application URL
    search_query = (
        f'"{company}" "{title}" (site:greenhouse.io OR site:lever.co OR '
        f'site:ashbyhq.com OR site:myworkdayjobs.com OR site:smartrecruiters.com OR apply careers)'
    )
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
            ignored_domains = ("linkedin.com", "naukri.com", "indeed.com", "duckduckgo.com")
            # Look for result link anchors
            for link in soup.select("a.result__url, a.result__a, a.result__snippet"):
                href = link.get("href")
                clean_href = _clean_ddg_url(href)
                if not clean_href or not clean_href.startswith("http"):
                    continue
                href_low = clean_href.lower()
                if any(ign in href_low for ign in ignored_domains):
                    continue
                # Found candidate URL
                register_discovered_board(clean_href)
                return clean_href
    except Exception as e:
        logger.debug("Failed to resolve direct apply URL via DuckDuckGo: %s", e)

    return fallback_url
