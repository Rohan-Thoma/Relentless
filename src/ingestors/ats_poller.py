from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import os
import re

import httpx

from src.config import load_config
from src.db import get_connection, make_dedup_hash
from src.dedupe import is_near_dup
from src.httpx_utils import get_with_retry, post_with_retry
from src.ingestors.common import (
    clean_html,
    insert_job,
    job_exists,
    matches_basic_filters,
    norm_loc,
    tokenize,
)
from src.ingestors.freshness import is_timestamp_fresh
from src.ingestors.gmail_ingestor import poll_gmail_alerts
from src.ingestors.jobboards_poller import poll_jobboards
from src.ingestors.location_policy import extract_job_location_facts
from src.ingestors.url_resolver import extract_public_hiring_email

logger = logging.getLogger(__name__)


def _cfg():
    return load_config()


def _tokenize(text: str):
    return tokenize(text)


def _norm_loc(loc: str) -> str:
    return norm_loc(loc)


def _max_ats_age_hours() -> float:
    cfg = _cfg()
    ingest_cfg = getattr(cfg, "ingestion", None)
    days = getattr(ingest_cfg, "max_job_age_days", 7) if ingest_cfg else 7
    return float(days) * 24.0 if days > 0 else 0.0


def _db_health_enabled() -> bool:
    return not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"))


def _get_board_health(platform: str, slug: str) -> dict | None:
    if not _db_health_enabled():
        return None
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT active, etag, consecutive_failures, matching_jobs_30d, next_poll_at
                FROM discovered_ats_boards
                WHERE platform = %s AND slug = %s;
                """,
                (platform, slug),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _should_skip_board(health: dict | None) -> bool:
    if not health:
        return False
    if health.get("active") is False:
        return True
    nxt = health.get("next_poll_at")
    if nxt is not None:
        if isinstance(nxt, str):
            try:
                nxt = datetime.fromisoformat(nxt.replace("Z", "+00:00"))
            except Exception:
                return False
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        if datetime.now(UTC) < nxt:
            return True
    return False


def _record_board_health(
    platform: str,
    slug: str,
    http_status: int,
    matched_count: int = 0,
    etag: str | None = None,
    company: str | None = None,
) -> None:
    """Self-pruning & yield-based backoff tracker on discovered_ats_boards:
    - 3 consecutive 404/410s -> auto-disable board (active=FALSE)
    - 0 matched jobs -> exponential backoff (24h -> 72h) to stop ATS doom loops
    - >0 matched jobs -> keep hot (next poll in 2h)
    """
    if not _db_health_enabled():
        return
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT consecutive_failures, matching_jobs_30d
                FROM discovered_ats_boards
                WHERE platform = %s AND slug = %s;
                """,
                (platform, slug),
            )
            row = cur.fetchone()
            prev_fail = int(row["consecutive_failures"] or 0) if row else 0
            prev_match = int(row["matching_jobs_30d"] or 0) if row else 0

            active = True
            if http_status in (404, 410):
                new_fail = prev_fail + 1
                if new_fail >= 3:
                    active = False
                    logger.warning(
                        "Auto-disabling dead ATS board %s/%s after %d consecutive HTTP %d responses",
                        platform,
                        slug,
                        new_fail,
                        http_status,
                    )
                next_poll = datetime.now(UTC) + timedelta(hours=24 * new_fail)
                new_match = prev_match
            elif http_status == 304:
                new_fail = 0
                new_match = prev_match
                next_poll = datetime.now(UTC) + timedelta(hours=4)
            elif http_status == 200:
                if matched_count > 0:
                    new_fail = 0
                    new_match = prev_match + matched_count
                    next_poll = datetime.now(UTC) + timedelta(hours=2)
                else:
                    # Track consecutive empty polls gently via backoff without disabling
                    new_fail = prev_fail + 1 if prev_match == 0 else 0
                    new_match = prev_match
                    backoff_hrs = 72 if new_fail >= 6 else (24 if new_fail >= 3 else 4)
                    next_poll = datetime.now(UTC) + timedelta(hours=backoff_hrs)
            else:
                new_fail = prev_fail + 1
                new_match = prev_match
                next_poll = datetime.now(UTC) + timedelta(hours=6)

            cur.execute(
                """
                INSERT INTO discovered_ats_boards (
                    platform, slug, company, last_polled_at, active, etag,
                    consecutive_failures, last_http_status, matching_jobs_30d, next_poll_at
                )
                VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, slug) DO UPDATE SET
                    last_polled_at = NOW(),
                    active = EXCLUDED.active,
                    etag = COALESCE(EXCLUDED.etag, discovered_ats_boards.etag),
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    last_http_status = EXCLUDED.last_http_status,
                    matching_jobs_30d = EXCLUDED.matching_jobs_30d,
                    next_poll_at = EXCLUDED.next_poll_at;
                """,
                (
                    platform,
                    slug,
                    company or slug.capitalize(),
                    active,
                    etag,
                    new_fail,
                    http_status,
                    new_match,
                    next_poll,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Could not record board health for %s/%s: %s", platform, slug, e)


async def _insert_job_async(conn, *args, **kwargs) -> bool:
    if conn is None:
        return await asyncio.to_thread(insert_job, None, *args, **kwargs)
    return insert_job(conn, *args, **kwargs)


def _extract_wm(title: str, loc: str, jd: str, is_remote: bool | None = None) -> str:
    facts = extract_job_location_facts(title=title, location=loc, jd_text=jd, is_remote_hint=is_remote)
    return facts.work_mode


async def poll_greenhouse(client: httpx.AsyncClient, conn, board: str) -> int:
    health = await asyncio.to_thread(_get_board_health, "greenhouse", board)
    if _should_skip_board(health):
        return 0
    headers = {}
    if health and health.get("etag"):
        headers["If-None-Match"] = health["etag"]

    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    resp = await get_with_retry(client, url, headers=headers or None)
    if resp.status_code == 304:
        await asyncio.to_thread(_record_board_health, "greenhouse", board, 304, 0, health.get("etag"))
        return 0
    if resp.status_code != 200:
        await asyncio.to_thread(_record_board_health, "greenhouse", board, resp.status_code, 0)
        return 0

    added = 0
    max_hrs = _max_ats_age_hours()
    for job in resp.json().get("jobs", []):
        if not is_timestamp_fresh(job.get("updated_at"), max_hrs):
            continue
        title = job.get("title", "")
        loc_name = job.get("location", {}).get("name", "") if isinstance(job.get("location"), dict) else ""
        offices = [o.get("name", "") for o in job.get("offices", []) if isinstance(o, dict)]
        loc = ", ".join(filter(None, [loc_name, *offices]))
        jd = clean_html(job.get("content", ""))
        if matches_basic_filters(title, loc, jd_text=jd):
            wm = _extract_wm(title, loc, jd)
            email_hint = extract_public_hiring_email(jd)
            ext_id = str(job.get("id")) if job.get("id") is not None else None
            if await _insert_job_async(
                conn,
                "greenhouse",
                board.capitalize(),
                title,
                loc,
                job.get("absolute_url", ""),
                jd,
                work_mode=wm,
                external_job_id=ext_id,
                hiring_contact_email=email_hint,
            ):
                added += 1

    await asyncio.to_thread(
        _record_board_health, "greenhouse", board, 200, added, resp.headers.get("etag")
    )
    return added


async def poll_lever(client: httpx.AsyncClient, conn, site: str) -> int:
    health = await asyncio.to_thread(_get_board_health, "lever", site)
    if _should_skip_board(health):
        return 0
    headers = {}
    if health and health.get("etag"):
        headers["If-None-Match"] = health["etag"]

    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    resp = await get_with_retry(client, url, headers=headers or None)
    if resp.status_code == 304:
        await asyncio.to_thread(_record_board_health, "lever", site, 304, 0, health.get("etag"))
        return 0
    if resp.status_code != 200:
        await asyncio.to_thread(_record_board_health, "lever", site, resp.status_code, 0)
        return 0

    added = 0
    max_hrs = _max_ats_age_hours()
    for job in resp.json():
        if not is_timestamp_fresh(job.get("createdAt"), max_hrs):
            continue
        title = job.get("text", "")
        cats = job.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        wp = job.get("workplaceType") or ""
        loc = ", ".join(filter(None, [cats.get("location", ""), *all_locs, wp]))
        jd = job.get("descriptionPlain", "")
        is_rem = str(wp).strip().lower() == "remote"
        if matches_basic_filters(title, loc, jd_text=jd, is_remote=True if is_rem else None):
            wm = _extract_wm(title, loc, jd, is_remote=True if is_rem else None)
            email_hint = extract_public_hiring_email(jd)
            ext_id = str(job.get("id")) if job.get("id") is not None else None
            if await _insert_job_async(
                conn,
                "lever",
                site.capitalize(),
                title,
                loc,
                job.get("hostedUrl", ""),
                jd,
                work_mode=wm,
                external_job_id=ext_id,
                hiring_contact_email=email_hint,
            ):
                added += 1

    await asyncio.to_thread(
        _record_board_health, "lever", site, 200, added, resp.headers.get("etag")
    )
    return added


async def poll_ashby(client: httpx.AsyncClient, conn, board: str) -> int:
    health = await asyncio.to_thread(_get_board_health, "ashby", board)
    if _should_skip_board(health):
        return 0
    headers = {}
    if health and health.get("etag"):
        headers["If-None-Match"] = health["etag"]

    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    resp = await get_with_retry(client, url, headers=headers or None)
    if resp.status_code == 304:
        await asyncio.to_thread(_record_board_health, "ashby", board, 304, 0, health.get("etag"))
        return 0
    if resp.status_code != 200:
        await asyncio.to_thread(_record_board_health, "ashby", board, resp.status_code, 0)
        return 0

    added = 0
    max_hrs = _max_ats_age_hours()
    for job in resp.json().get("jobs", []):
        if not is_timestamp_fresh(job.get("publishedAt"), max_hrs):
            continue
        title = job.get("title", "")
        loc_primary = job.get("location", "")
        sec_locs = [s.get("location", "") for s in job.get("secondaryLocations", []) if isinstance(s, dict)]
        is_rem = bool(job.get("isRemote"))
        is_remote_str = "Remote" if is_rem else ""
        wp = job.get("workplaceType") or ""
        loc = ", ".join(filter(None, [loc_primary, *sec_locs, is_remote_str, wp]))
        jd = clean_html(job.get("descriptionHtml", ""))
        if matches_basic_filters(title, loc, jd_text=jd, is_remote=True if is_rem else None):
            wm = _extract_wm(title, loc, jd, is_remote=True if is_rem else None)
            email_hint = extract_public_hiring_email(jd)
            ext_id = str(job.get("id")) if job.get("id") is not None else None
            if await _insert_job_async(
                conn,
                "ashby",
                board.capitalize(),
                title,
                loc,
                job.get("jobUrl", ""),
                jd,
                work_mode=wm,
                external_job_id=ext_id,
                hiring_contact_email=email_hint,
            ):
                added += 1

    await asyncio.to_thread(
        _record_board_health, "ashby", board, 200, added, resp.headers.get("etag")
    )
    return added


def _workday_search_terms() -> list[str]:
    """Builds concise single-phrase Workday CXS queries (NO broken Lucene 'OR' syntax!)."""
    cfg = _cfg()
    terms = []
    for t in (cfg.jobboards.search_terms or [])[:3]:
        clean = t.strip()
        if clean and clean not in terms:
            terms.append(clean)
    return terms or ["Machine Learning", "AI Engineer", "Python"]


def _title_passes_keywords(title: str) -> bool:
    """Fast title-only pre-check before Workday detail fetch (for 'N Locations' items)."""
    prefs = _cfg().candidate_preferences
    t_low = (title or "").lower()
    for bad in prefs.exclude_title_keywords:
        if re.search(r"\b" + re.escape(bad.lower()) + r"\b", t_low):
            return False
    tech_tokens = {"c++", "c#", ".net", "node.js", "ai", "ml"}
    for good in prefs.target_role_keywords:
        g = good.lower()
        pat = (
            r"(?:^|[^a-z0-9])" + re.escape(g) + r"(?:$|[^a-z0-9])"
            if g in tech_tokens
            else r"\b" + re.escape(g) + r"\b"
        )
        if re.search(pat, t_low):
            return True
    return False


async def poll_workday(
    client: httpx.AsyncClient,
    conn,
    cfg: dict,
    max_pages: int = 3,
) -> int:
    """Bounded Workday CXS poller:
    - Uses clean single-phrase searchText (no broken Lucene OR)
    - Caps pagination at max_pages per term so large tenants never stall the pipeline
    - Resolves Workday 'N Locations' multi-office strings via detail endpoint before rejecting!
    """
    company = cfg["company"]
    base = f"https://{cfg['tenant']}.{cfg['wd_instance']}.myworkdayjobs.com/wday/cxs/{cfg['tenant']}/{cfg['site']}"
    list_url = f"{base}/jobs"
    added = 0
    limit = 20
    max_hrs = _max_ats_age_hours()
    seen_paths: set[str] = set()
    search_terms = _workday_search_terms()

    for search_text in search_terms:
        offset = 0
        total = None
        for _page in range(max_pages):
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": search_text,
            }
            try:
                resp = await post_with_retry(
                    client, list_url, json=payload, headers={"Accept": "application/json"}
                )
                if resp.status_code != 200:
                    break
            except Exception:
                break

            data = resp.json()
            postings = data.get("jobPostings", [])
            if total is None:
                total = data.get("total", len(postings))
            if not postings:
                break

            for item in postings:
                ext_path = item.get("externalPath", "")
                if not ext_path or ext_path in seen_paths:
                    continue
                seen_paths.add(ext_path)

                if not is_timestamp_fresh(item.get("postedOn"), max_hrs):
                    continue
                title = item.get("title", "")
                loc = item.get("locationsText", "") or ""
                is_multi_loc_placeholder = bool(re.match(r"^\d+\s+locations?$", loc.strip(), re.IGNORECASE))

                if is_multi_loc_placeholder:
                    # Don't drop '3 Locations' before fetching detail endpoint!
                    if not _title_passes_keywords(title):
                        continue
                else:
                    if not matches_basic_filters(title, loc):
                        continue

                if (conn is not None and is_near_dup(conn, company, title)) or job_exists(
                    conn, make_dedup_hash(company, title, loc)
                ):
                    continue

                detail_resp = await get_with_retry(
                    client, f"{base}{ext_path}", headers={"Accept": "application/json"}
                )
                if detail_resp.status_code == 200:
                    info = detail_resp.json().get("jobPostingInfo", {})
                    jd = clean_html(info.get("jobDescription", ""))
                    detail_locs = []
                    if info.get("location"):
                        detail_locs.append(str(info["location"]))
                    for al in info.get("additionalLocations") or []:
                        if isinstance(al, str) and al.strip():
                            detail_locs.append(al.strip())
                    if info.get("remoteType"):
                        detail_locs.append(str(info["remoteType"]))
                    resolved_loc = ", ".join(dict.fromkeys(detail_locs)) if detail_locs else loc

                    # Re-check with full resolved locations + full JD text
                    if not matches_basic_filters(title, resolved_loc, jd_text=jd):
                        continue

                    apply_url = (
                        info.get("externalUrl")
                        or f"https://{cfg['tenant']}.{cfg['wd_instance']}.myworkdayjobs.com/{cfg['site']}{ext_path}"
                    )
                    wm = _extract_wm(title, resolved_loc, jd)
                    email_hint = extract_public_hiring_email(jd)
                    ext_id = str(info.get("jobReqId") or ext_path)
                    if await _insert_job_async(
                        conn,
                        "workday",
                        company,
                        title,
                        resolved_loc,
                        apply_url,
                        jd,
                        work_mode=wm,
                        external_job_id=ext_id,
                        hiring_contact_email=email_hint,
                    ):
                        added += 1

            offset += limit
            if offset >= (total or 0):
                break
    return added


async def poll_smartrecruiters(client: httpx.AsyncClient, conn, company: str) -> int:
    health = await asyncio.to_thread(_get_board_health, "smartrecruiters", company)
    if _should_skip_board(health):
        return 0
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        await asyncio.to_thread(_record_board_health, "smartrecruiters", company, resp.status_code, 0)
        return 0
    added = 0
    max_hrs = _max_ats_age_hours()
    data = resp.json()
    postings = data.get("content", []) if isinstance(data, dict) else []
    for job in postings:
        if not is_timestamp_fresh(job.get("releasedDate"), max_hrs):
            continue
        title = job.get("name", "")
        job_id = job.get("id", "")
        loc_data = job.get("location") if isinstance(job.get("location"), dict) else {}
        city = loc_data.get("city", "")
        region = loc_data.get("region", "")
        country = loc_data.get("country", "")
        if country.lower() == "in":
            country = "India"
        is_rem = bool(loc_data.get("remote"))
        is_remote_str = "Remote" if is_rem else ""
        loc = ", ".join(filter(None, [city, region, country, is_remote_str]))

        if not matches_basic_filters(title, loc, is_remote=True if is_rem else None):
            continue

        dedup_hash = make_dedup_hash(company, title, loc)
        if (conn is not None and is_near_dup(conn, company, title)) or job_exists(conn, dedup_hash):
            continue

        jd = ""
        apply_url = ""
        if job_id:
            detail_url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id}"
            det_resp = await get_with_retry(client, detail_url)
            if det_resp.status_code == 200:
                det_data = det_resp.json()
                sections = det_data.get("jobAd", {}).get("sections", {})
                desc_text = sections.get("jobDescription", {}).get("text", "")
                qual_text = sections.get("qualifications", {}).get("text", "")
                addl_text = sections.get("additionalInformation", {}).get("text", "")
                jd = clean_html("\n\n".join(filter(None, [desc_text, qual_text, addl_text])))
                apply_url = det_data.get("applyUrl", "")

        if not apply_url:
            apply_url = (
                job.get("applyUrl", "")
                or job.get("postingUrl", "")
                or f"https://jobs.smartrecruiters.com/{company}/{job_id}"
            )
        if not jd:
            jd = clean_html(
                job.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "")
                if isinstance(job.get("jobAd"), dict)
                else ""
            )

        if not matches_basic_filters(title, loc, jd_text=jd, is_remote=True if is_rem else None):
            continue

        wm = _extract_wm(title, loc, jd, is_remote=True if is_rem else None)
        email_hint = extract_public_hiring_email(jd)
        if await _insert_job_async(
            conn,
            "smartrecruiters",
            company.capitalize(),
            title,
            loc,
            apply_url,
            jd,
            work_mode=wm,
            external_job_id=str(job_id) if job_id else None,
            hiring_contact_email=email_hint,
        ):
            added += 1

    await asyncio.to_thread(_record_board_health, "smartrecruiters", company, 200, added)
    return added


async def poll_recruitee(client: httpx.AsyncClient, conn, company: str) -> int:
    health = await asyncio.to_thread(_get_board_health, "recruitee", company)
    if _should_skip_board(health):
        return 0
    url = f"https://{company}.recruitee.com/api/offers/"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        await asyncio.to_thread(_record_board_health, "recruitee", company, resp.status_code, 0)
        return 0
    added = 0
    max_hrs = _max_ats_age_hours()
    data = resp.json()
    offers = data.get("offers", []) if isinstance(data, dict) else []
    for job in offers:
        if not is_timestamp_fresh(job.get("created_at"), max_hrs):
            continue
        title = job.get("title", "")
        loc_str = job.get("location") or ""
        city = job.get("city") or ""
        country = job.get("country") or ""
        if country.lower() == "in":
            country = "India"
        is_rem = bool(job.get("remote"))
        is_remote_str = "(Remote)" if is_rem else ""
        loc = ", ".join(filter(None, [loc_str, city, country, is_remote_str]))
        jd = clean_html(job.get("description", ""))
        if matches_basic_filters(title, loc, jd_text=jd, is_remote=True if is_rem else None):
            apply_url = job.get("careers_url", "") or job.get("url", "")
            wm = _extract_wm(title, loc, jd, is_remote=True if is_rem else None)
            email_hint = extract_public_hiring_email(jd)
            ext_id = str(job.get("id")) if job.get("id") is not None else None
            if await _insert_job_async(
                conn,
                "recruitee",
                company.capitalize(),
                title,
                loc,
                apply_url,
                jd,
                work_mode=wm,
                external_job_id=ext_id,
                hiring_contact_email=email_hint,
            ):
                added += 1

    await asyncio.to_thread(_record_board_health, "recruitee", company, 200, added)
    return added


async def poll_remotive(client: httpx.AsyncClient, conn) -> int:
    url = "https://remotive.com/api/remote-jobs?category=software-dev"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    max_hrs = _max_ats_age_hours()
    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    for job in jobs:
        if not is_timestamp_fresh(job.get("publication_date"), max_hrs):
            continue
        title = job.get("title", "")
        company = job.get("company_name", "RemotiveCompany")
        loc = job.get("candidate_required_location") or "Remote"
        jd = clean_html(job.get("description", ""))
        if matches_basic_filters(title, loc, jd_text=jd, is_remote=True):
            apply_url = job.get("url", "")
            email_hint = extract_public_hiring_email(jd)
            ext_id = str(job.get("id")) if job.get("id") is not None else None
            if await _insert_job_async(
                conn,
                "remotive",
                company,
                title,
                loc,
                apply_url,
                jd,
                work_mode="remote",
                external_job_id=ext_id,
                hiring_contact_email=email_hint,
            ):
                added += 1
    return added


async def poll_arbeitnow(client: httpx.AsyncClient, conn) -> int:
    url = "https://www.arbeitnow.com/api/job-board-api"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    max_hrs = _max_ats_age_hours()
    data = resp.json()
    jobs = data.get("data", []) if isinstance(data, dict) else []
    for job in jobs:
        if not is_timestamp_fresh(job.get("created_at"), max_hrs):
            continue
        title = job.get("title", "")
        company = job.get("company_name", "ArbeitnowCompany")
        loc_primary = job.get("location", "")
        is_rem = bool(job.get("remote"))
        is_remote_str = "Remote" if is_rem else ""
        loc = ", ".join(filter(None, [loc_primary, is_remote_str])) or "Remote"
        jd = clean_html(job.get("description", ""))
        if matches_basic_filters(title, loc, jd_text=jd, is_remote=True if is_rem else None):
            apply_url = job.get("url", "")
            wm = _extract_wm(title, loc, jd, is_remote=True if is_rem else None)
            email_hint = extract_public_hiring_email(jd)
            ext_id = str(job.get("slug")) if job.get("slug") else None
            if await _insert_job_async(
                conn,
                "arbeitnow",
                company,
                title,
                loc,
                apply_url,
                jd,
                work_mode=wm,
                external_job_id=ext_id,
                hiring_contact_email=email_hint,
            ):
                added += 1
    return added


def load_discovered_boards(conn=None) -> dict[str, list]:
    discovered: dict[str, list] = {
        "greenhouse": [],
        "lever": [],
        "ashby": [],
        "workday_cxs": [],
        "smartrecruiters": [],
        "recruitee": [],
    }

    def _query(c):
        with c.cursor() as cur:
            cur.execute(
                "SELECT platform, slug, company, metadata FROM discovered_ats_boards WHERE active = TRUE;"
            )
            for row in cur.fetchall():
                platform = row.get("platform")
                slug = row.get("slug")
                meta = row.get("metadata") or {}
                if platform == "workday_cxs":
                    if meta.get("tenant") and meta.get("wd_instance") and meta.get("site"):
                        discovered["workday_cxs"].append({
                            "company": meta.get("company")
                            or row.get("company")
                            or meta["tenant"].capitalize(),
                            "tenant": meta["tenant"],
                            "wd_instance": meta["wd_instance"],
                            "site": meta["site"],
                        })
                    elif slug and ":" in slug:
                        parts = slug.split(":")
                        if len(parts) == 3:
                            discovered["workday_cxs"].append({
                                "company": row.get("company") or parts[0].capitalize(),
                                "tenant": parts[0],
                                "wd_instance": parts[1],
                                "site": parts[2],
                            })
                elif platform in discovered and slug:
                    discovered[platform].append(slug)

    if conn is not None:
        try:
            _query(conn)
        except Exception as e:
            logger.warning("Could not query discovered boards: %s", e)
    elif not os.getenv("PYTEST_CURRENT_TEST") or os.getenv("RELENTLESS_TEST_LIVE_DB"):
        try:
            with get_connection() as c:
                _query(c)
        except Exception as e:
            logger.warning("Could not load discovered boards: %s", e)

    return discovered


async def poll_all_scraping(sources: list[str] | None = None) -> int:
    """High-priority scraping ingestion: JobSpy multi-board search + Gmail job alerts."""
    cfg = _cfg()
    ingest_cfg = getattr(cfg, "ingestion", None)
    active_sources = {s.lower() for s in (sources or ["jobboards", "gmail"])}
    total = 0

    if "jobboards" in active_sources and (ingest_cfg is None or ingest_cfg.enable_jobboards):
        try:
            print("  🌐 [Phase 1.1] Querying JobSpy direct job boards...", flush=True)
            jb_new = await poll_jobboards()
            print(f"  ✅ [Phase 1.1 Complete] JobSpy: +{jb_new} new matching jobs added.", flush=True)
            if isinstance(jb_new, int):
                total += jb_new
        except Exception as e:
            print(f"  ❌ JobSpy ingestion failed: {e}", flush=True)
            logger.warning("JobSpy ingestion failed: %s", e)

    if "gmail" in active_sources and (ingest_cfg is None or ingest_cfg.enable_gmail):
        try:
            print("  📧 [Phase 1.2] Scanning Gmail inbox for job alerts...", flush=True)
            gmail_new = await poll_gmail_alerts()
            print(f"  ✅ [Phase 1.2 Complete] Gmail Alerts: +{gmail_new} new matching jobs added.", flush=True)
            if isinstance(gmail_new, int):
                total += gmail_new
        except Exception as e:
            print(f"  ❌ Gmail alert scanner failed: {e}", flush=True)
            logger.warning("Gmail alert scanner failed: %s", e)

    return total


async def poll_all_ats(platforms: list[str] | None = None) -> int:
    """Smart, self-pruning ATS crawler with ETag caching, yield backoff, and bounded Workday."""
    cfg = _cfg()
    ats = cfg.ats_targets
    ingest_cfg = cfg.ingestion
    targets = {
        "greenhouse": list(ats.greenhouse),
        "lever": list(ats.lever),
        "ashby": list(ats.ashby),
        "workday_cxs": [w.model_dump() for w in ats.workday_cxs],
        "smartrecruiters": list(ats.smartrecruiters),
        "recruitee": list(ats.recruitee),
    }

    discovered = load_discovered_boards()
    for plat in ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"):
        existing_set = {s.lower() for s in targets[plat]}
        for slug in discovered.get(plat, []):
            if slug.lower() not in existing_set:
                targets[plat].append(slug)
                existing_set.add(slug.lower())

    existing_wd_keys = {
        (w.get("tenant"), w.get("wd_instance"), w.get("site"))
        for w in targets["workday_cxs"]
    }
    for w in discovered.get("workday_cxs", []):
        wd_key = (w.get("tenant"), w.get("wd_instance"), w.get("site"))
        if wd_key not in existing_wd_keys:
            targets["workday_cxs"].append(w)
            existing_wd_keys.add(wd_key)

    active_plats = {p.lower() for p in platforms} if platforms else None

    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/2.0)"}
    sem = asyncio.Semaphore(20)

    async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
        tasks = []

        async def _run_poll(poll_fn, name, *args, timeout_sec: float = 20.0):
            async with sem:
                try:
                    n = await asyncio.wait_for(poll_fn(client, None, *args), timeout=timeout_sec)
                    if n > 0:
                        label = args[0]["company"] if (args and isinstance(args[0], dict)) else (args[0] if args else "")
                        print(f"  [{name}] {label}: +{n} new matching jobs")
                    return n
                except TimeoutError:
                    logger.warning("Timed out polling [%s] %s after %.0fs", name, args, timeout_sec)
                    return 0
                except Exception as e:
                    logger.warning("Error polling [%s] %s: %s", name, args, e)
                    return 0

        if not active_plats or "greenhouse" in active_plats:
            for b in targets.get("greenhouse", []):
                tasks.append(_run_poll(poll_greenhouse, "Greenhouse", b))
        if not active_plats or "lever" in active_plats:
            for s in targets.get("lever", []):
                tasks.append(_run_poll(poll_lever, "Lever", s))
        if not active_plats or "ashby" in active_plats:
            for a in targets.get("ashby", []):
                tasks.append(_run_poll(poll_ashby, "Ashby", a))
        if (active_plats and ("workday" in active_plats or "workday_cxs" in active_plats)) or (
            not active_plats and ingest_cfg.enable_workday
        ):
            for w in targets.get("workday_cxs", []):
                tasks.append(_run_poll(poll_workday, "Workday", w, timeout_sec=15.0))
        if not active_plats or "smartrecruiters" in active_plats:
            for s in targets.get("smartrecruiters", []):
                tasks.append(_run_poll(poll_smartrecruiters, "SmartRecruiters", s))
        if not active_plats or "recruitee" in active_plats:
            for r in targets.get("recruitee", []):
                tasks.append(_run_poll(poll_recruitee, "Recruitee", r))

        if (active_plats and "remotive" in active_plats) or (
            not active_plats and ingest_cfg.enable_remote_feeds
        ):
            tasks.append(_run_poll(poll_remotive, "Remotive"))
        if (active_plats and "arbeitnow" in active_plats) or (
            not active_plats and ingest_cfg.enable_remote_feeds
        ):
            tasks.append(_run_poll(poll_arbeitnow, "Arbeitnow"))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_new = sum(r for r in results if isinstance(r, int))
        return total_new


async def run_ingestion(
    sources: list[str] | None = None,
    skip_ats: bool = False,
    scrape_only: bool = False,
    ats_only: bool = False,
    prioritize_scraping: bool = True,
) -> int:
    """Unified Ingestion Runner respecting config toggles and CLI flags cleanly."""
    cfg = _cfg()
    ingest_cfg = cfg.ingestion

    env_scrape_only = os.getenv("RELENTLESS_SCRAPE_ONLY", "").lower() in ("1", "true", "yes")
    env_skip_ats = os.getenv("RELENTLESS_SKIP_ATS", "").lower() in ("1", "true", "yes")

    do_scrape_only = scrape_only or env_scrape_only or ingest_cfg.scrape_only
    do_skip_ats = skip_ats or env_skip_ats or not ingest_cfg.enable_ats or do_scrape_only
    do_ats_only = ats_only

    total_new = 0

    if prioritize_scraping:
        if not do_ats_only:
            print("\n⚡ [1/2] Fast Scraping: Polling JobSpy multi-board & Gmail alerts...")
            scrape_new = await poll_all_scraping(sources=sources)
            total_new += scrape_new

        if not do_skip_ats and not do_scrape_only:
            print("\n🐢 [2/2] Direct ATS: Scanning Greenhouse, Lever, Ashby, Remote Feeds...")
            ats_new = await poll_all_ats()
            total_new += ats_new
        elif do_scrape_only:
            print("⏩ ATS polling skipped (Scrape-only mode active).")
    else:
        if not do_skip_ats and not do_scrape_only:
            ats_new = await poll_all_ats()
            total_new += ats_new
        if not do_ats_only:
            scrape_new = await poll_all_scraping(sources=sources)
            total_new += scrape_new

    print(f"\n🚀 Ingestion complete! {total_new} brand-new jobs saved to Neon PostgreSQL.")
    return total_new


if __name__ == "__main__":
    asyncio.run(run_ingestion())
