import asyncio
import logging
import os

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


async def _insert_job_async(conn, *args, **kwargs) -> bool:
    if conn is None:
        return await asyncio.to_thread(insert_job, None, *args, **kwargs)
    return insert_job(conn, *args, **kwargs)


async def poll_greenhouse(client: httpx.AsyncClient, conn, board: str) -> int:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
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
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("content", ""))
            if await _insert_job_async(conn, "greenhouse", board.capitalize(), title, loc, job.get("absolute_url", ""), jd):
                added += 1
    return added


async def poll_lever(client: httpx.AsyncClient, conn, site: str) -> int:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
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
        if matches_basic_filters(title, loc):
            jd = job.get("descriptionPlain", "")
            if await _insert_job_async(conn, "lever", site.capitalize(), title, loc, job.get("hostedUrl", ""), jd):
                added += 1
    return added


async def poll_ashby(client: httpx.AsyncClient, conn, board: str) -> int:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    max_hrs = _max_ats_age_hours()
    for job in resp.json().get("jobs", []):
        if not is_timestamp_fresh(job.get("publishedAt"), max_hrs):
            continue
        title = job.get("title", "")
        loc_primary = job.get("location", "")
        sec_locs = [s.get("location", "") for s in job.get("secondaryLocations", []) if isinstance(s, dict)]
        is_remote = "Remote" if job.get("isRemote") else ""
        wp = job.get("workplaceType") or ""
        loc = ", ".join(filter(None, [loc_primary, *sec_locs, is_remote, wp]))
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("descriptionHtml", ""))
            if await _insert_job_async(conn, "ashby", board.capitalize(), title, loc, job.get("jobUrl", ""), jd):
                added += 1
    return added


async def poll_workday(client: httpx.AsyncClient, conn, cfg: dict) -> int:
    company = cfg["company"]
    base = f"https://{cfg['tenant']}.{cfg['wd_instance']}.myworkdayjobs.com/wday/cxs/{cfg['tenant']}/{cfg['site']}"
    list_url = f"{base}/jobs"
    added = 0
    offset = 0
    limit = 20
    total = None
    max_hrs = _max_ats_age_hours()
    while True:
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": "AI OR Machine Learning OR Python OR Backend",
        }
        try:
            resp = await post_with_retry(client, list_url, json=payload, headers={"Accept": "application/json"})
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
            if not is_timestamp_fresh(item.get("postedOn"), max_hrs):
                continue
            title = item.get("title", "")
            loc = item.get("locationsText", "")
            ext_path = item.get("externalPath", "")
            if not ext_path or not matches_basic_filters(title, loc):
                continue
            if (conn is not None and is_near_dup(conn, company, title)) or job_exists(
                conn, make_dedup_hash(company, title, loc)
            ):
                continue
            detail_resp = await get_with_retry(client, f"{base}{ext_path}", headers={"Accept": "application/json"})
            if detail_resp.status_code == 200:
                info = detail_resp.json().get("jobPostingInfo", {})
                jd = clean_html(info.get("jobDescription", ""))
                apply_url = (
                    info.get("externalUrl")
                    or f"https://{cfg['tenant']}.{cfg['wd_instance']}.myworkdayjobs.com/{cfg['site']}{ext_path}"
                )
                if await _insert_job_async(conn, "workday", company, title, loc, apply_url, jd):
                    added += 1
        offset += limit
        if offset >= (total or 0):
            break
    return added


async def poll_smartrecruiters(client: httpx.AsyncClient, conn, company: str) -> int:
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
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
        is_remote = "Remote" if loc_data.get("remote") else ""
        loc = ", ".join(filter(None, [city, region, country, is_remote]))

        if not matches_basic_filters(title, loc):
            continue

        dedup_hash = make_dedup_hash(company, title, loc)
        if (conn is not None and is_near_dup(conn, company, title)) or job_exists(conn, dedup_hash):
            continue

        # Fetch detail endpoint for full JD and applyUrl
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

        if await _insert_job_async(conn, "smartrecruiters", company.capitalize(), title, loc, apply_url, jd):
            added += 1
    return added


async def poll_recruitee(client: httpx.AsyncClient, conn, company: str) -> int:
    url = f"https://{company}.recruitee.com/api/offers/"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
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
        is_remote = "(Remote)" if job.get("remote") else ""
        loc = ", ".join(filter(None, [loc_str, city, country, is_remote]))
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("description", ""))
            apply_url = job.get("careers_url", "") or job.get("url", "")
            if await _insert_job_async(conn, "recruitee", company.capitalize(), title, loc, apply_url, jd):
                added += 1
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
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("description", ""))
            apply_url = job.get("url", "")
            if await _insert_job_async(conn, "remotive", company, title, loc, apply_url, jd):
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
        is_remote = "Remote" if job.get("remote") else ""
        loc = ", ".join(filter(None, [loc_primary, is_remote])) or "Remote"
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("description", ""))
            apply_url = job.get("url", "")
            if await _insert_job_async(conn, "arbeitnow", company, title, loc, apply_url, jd):
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
    """High-priority scraping ingestion:

    Runs JobSpy multi-board search (Naukri, LinkedIn, Indeed, Google) and Gmail job alerts.
    """
    cfg = _cfg()
    ingest_cfg = getattr(cfg, "ingestion", None)
    active_sources = {s.lower() for s in (sources or ["jobboards", "gmail"])}
    total = 0

    if "jobboards" in active_sources and (ingest_cfg is None or ingest_cfg.enable_jobboards):
        try:
            print("  🌐 [Phase 1.1] Querying JobSpy direct job boards (Naukri, LinkedIn, Indeed)...", flush=True)
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
    """Leisurely ATS crawling:

    Scans company ATS targets (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Recruitee, Remotive, Arbeitnow)
    plus dynamically discovered boards from the database.
    """
    ats = _cfg().ats_targets
    targets = {
        "greenhouse": list(ats.greenhouse),
        "lever": list(ats.lever),
        "ashby": list(ats.ashby),
        "workday_cxs": [w.model_dump() for w in ats.workday_cxs],
        "smartrecruiters": list(ats.smartrecruiters),
        "recruitee": list(ats.recruitee),
    }

    # Merge dynamically discovered boards
    discovered = load_discovered_boards()
    for plat in ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"):
        existing_set = set(targets[plat])
        for slug in discovered.get(plat, []):
            if slug not in existing_set:
                targets[plat].append(slug)
                existing_set.add(slug)

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

    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/1.0)"}
    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        tasks = []

        async def _run_poll(poll_fn, name, *args):
            async with sem:
                try:
                    n = await poll_fn(client, None, *args)
                    print(f"  [{name}] {args[0] if args else ''}: +{n} new matching jobs")
                    return n
                except Exception as e:
                    logger.warning("Error polling [%s] %s: %s", name, args, e)
                    print(f"  [{name}] {args[0] if args else ''}: error {e}")
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
        if not active_plats or "workday" in active_plats or "workday_cxs" in active_plats:
            for w in targets.get("workday_cxs", []):
                tasks.append(_run_poll(poll_workday, "Workday", w))
        if not active_plats or "smartrecruiters" in active_plats:
            for s in targets.get("smartrecruiters", []):
                tasks.append(_run_poll(poll_smartrecruiters, "SmartRecruiters", s))
        if not active_plats or "recruitee" in active_plats:
            for r in targets.get("recruitee", []):
                tasks.append(_run_poll(poll_recruitee, "Recruitee", r))

        if not active_plats or "remotive" in active_plats:
            tasks.append(_run_poll(poll_remotive, "Remotive"))
        if not active_plats or "arbeitnow" in active_plats:
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
    """Unified Ingestion Runner.

    Default: Prioritizes fast scraping (JobSpy + Gmail) before scanning ATS targets.
    Supports scrape_only / skip_ats to bypass slow ATS boards entirely.
    """
    cfg = _cfg()
    ingest_cfg = getattr(cfg, "ingestion", None)

    env_scrape_only = os.getenv("RELENTLESS_SCRAPE_ONLY", "").lower() in ("1", "true", "yes")
    env_skip_ats = os.getenv("RELENTLESS_SKIP_ATS", "").lower() in ("1", "true", "yes")

    cfg_scrape_only = getattr(ingest_cfg, "scrape_only", False) if ingest_cfg else False
    cfg_enable_ats = getattr(ingest_cfg, "enable_ats", True) if ingest_cfg else True

    do_scrape_only = scrape_only or env_scrape_only or cfg_scrape_only
    do_skip_ats = skip_ats or env_skip_ats or not cfg_enable_ats or do_scrape_only
    do_ats_only = ats_only

    total_new = 0

    if prioritize_scraping:
        # Phase 1: High-Speed Scraping (JobSpy + Gmail)
        if not do_ats_only:
            print("\n⚡ [1/2] Fast Scraping: Polling JobSpy multi-board & Gmail alerts...")
            scrape_new = await poll_all_scraping(sources=sources)
            total_new += scrape_new

        # Phase 2: Leisurely ATS Boards
        if not do_skip_ats and not do_scrape_only:
            print("\n🐢 [2/2] Leisurely ATS: Scanning Greenhouse, Lever, Ashby, Workday...")
            ats_new = await poll_all_ats()
            total_new += ats_new
        elif do_scrape_only:
            print("⏩ ATS polling skipped (Scrape-only mode active).")
    else:
        # Secondary order: ATS first, scraping second
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
