import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from src.config import load_config
from src.db import get_connection, make_dedup_hash
from src.dedupe import is_near_dup, normalize_title
from src.httpx_utils import get_with_retry, post_with_retry

logger = logging.getLogger(__name__)


def _cfg():
    return load_config()


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)


def _tokenize(text: str):
    return re.findall(r"[a-z0-9+#\.]+", (text or "").lower())


def _norm_loc(loc: str) -> str:
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
    l_low = _norm_loc(location or "")

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
) -> bool:
    dedup_hash = make_dedup_hash(company, title, location)
    if conn is not None and is_near_dup(conn, company, title):
        return False
    canonical = normalize_title(title)
    if conn is None:
        return True
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (dedup_hash, source_ats, company, title, location, apply_url, jd_text, canonical_title)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dedup_hash) DO NOTHING
            RETURNING id;
            """,
            (dedup_hash, source_ats, company, title, location, apply_url, jd_text, canonical),
        )
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


async def poll_greenhouse(client: httpx.AsyncClient, conn, board: str) -> int:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    for job in resp.json().get("jobs", []):
        title = job.get("title", "")
        loc_name = job.get("location", {}).get("name", "") if isinstance(job.get("location"), dict) else ""
        offices = [o.get("name", "") for o in job.get("offices", []) if isinstance(o, dict)]
        loc = ", ".join(filter(None, [loc_name, *offices]))
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("content", ""))
            if insert_job(conn, "greenhouse", board.capitalize(), title, loc, job.get("absolute_url", ""), jd):
                added += 1
    return added


async def poll_lever(client: httpx.AsyncClient, conn, site: str) -> int:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    for job in resp.json():
        title = job.get("text", "")
        cats = job.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        wp = job.get("workplaceType") or ""
        loc = ", ".join(filter(None, [cats.get("location", ""), *all_locs, wp]))
        if matches_basic_filters(title, loc):
            jd = job.get("descriptionPlain", "")
            if insert_job(conn, "lever", site.capitalize(), title, loc, job.get("hostedUrl", ""), jd):
                added += 1
    return added


async def poll_ashby(client: httpx.AsyncClient, conn, board: str) -> int:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    for job in resp.json().get("jobs", []):
        title = job.get("title", "")
        loc_primary = job.get("location", "")
        sec_locs = [s.get("location", "") for s in job.get("secondaryLocations", []) if isinstance(s, dict)]
        is_remote = "Remote" if job.get("isRemote") else ""
        wp = job.get("workplaceType") or ""
        loc = ", ".join(filter(None, [loc_primary, *sec_locs, is_remote, wp]))
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("descriptionHtml", ""))
            if insert_job(conn, "ashby", board.capitalize(), title, loc, job.get("jobUrl", ""), jd):
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
                if insert_job(conn, "workday", company, title, loc, apply_url, jd):
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
    data = resp.json()
    postings = data.get("content", []) if isinstance(data, dict) else []
    for job in postings:
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

        if insert_job(conn, "smartrecruiters", company.capitalize(), title, loc, apply_url, jd):
            added += 1
    return added


async def poll_recruitee(client: httpx.AsyncClient, conn, company: str) -> int:
    url = f"https://{company}.recruitee.com/api/offers/"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    data = resp.json()
    offers = data.get("offers", []) if isinstance(data, dict) else []
    for job in offers:
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
            if insert_job(conn, "recruitee", company.capitalize(), title, loc, apply_url, jd):
                added += 1
    return added


async def poll_remotive(client: httpx.AsyncClient, conn) -> int:
    url = "https://remotive.com/api/remote-jobs?category=software-dev"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    for job in jobs:
        title = job.get("title", "")
        company = job.get("company_name", "RemotiveCompany")
        loc = job.get("candidate_required_location") or "Remote"
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("description", ""))
            apply_url = job.get("url", "")
            if insert_job(conn, "remotive", company, title, loc, apply_url, jd):
                added += 1
    return added


async def poll_arbeitnow(client: httpx.AsyncClient, conn) -> int:
    url = "https://www.arbeitnow.com/api/job-board-api"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    data = resp.json()
    jobs = data.get("data", []) if isinstance(data, dict) else []
    for job in jobs:
        title = job.get("title", "")
        company = job.get("company_name", "ArbeitnowCompany")
        loc_primary = job.get("location", "")
        is_remote = "Remote" if job.get("remote") else ""
        loc = ", ".join(filter(None, [loc_primary, is_remote])) or "Remote"
        if matches_basic_filters(title, loc):
            jd = clean_html(job.get("description", ""))
            apply_url = job.get("url", "")
            if insert_job(conn, "arbeitnow", company, title, loc, apply_url, jd):
                added += 1
    return added


async def run_ingestion():
    ats = _cfg().ats_targets
    targets = {
        "greenhouse": ats.greenhouse,
        "lever": ats.lever,
        "ashby": ats.ashby,
        "workday_cxs": [w.model_dump() for w in ats.workday_cxs],
        "smartrecruiters": ats.smartrecruiters,
        "recruitee": ats.recruitee,
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/1.0)"}
    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        tasks = []

        async def _run_poll(poll_fn, name, *args):
            async with sem:
                try:
                    with get_connection() as conn:
                        n = await poll_fn(client, conn, *args)
                        print(f"  [{name}] {args[0] if args else ''}: +{n} new matching jobs")
                        return n
                except Exception as e:
                    logger.warning("Error polling [%s] %s: %s", name, args, e)
                    print(f"  [{name}] {args[0] if args else ''}: error {e}")
                    return 0

        for b in targets.get("greenhouse", []):
            tasks.append(_run_poll(poll_greenhouse, "Greenhouse", b))
        for s in targets.get("lever", []):
            tasks.append(_run_poll(poll_lever, "Lever", s))
        for a in targets.get("ashby", []):
            tasks.append(_run_poll(poll_ashby, "Ashby", a))
        for w in targets.get("workday_cxs", []):
            tasks.append(_run_poll(poll_workday, "Workday", w))
        for s in targets.get("smartrecruiters", []):
            tasks.append(_run_poll(poll_smartrecruiters, "SmartRecruiters", s))
        for r in targets.get("recruitee", []):
            tasks.append(_run_poll(poll_recruitee, "Recruitee", r))

        tasks.append(_run_poll(poll_remotive, "Remotive"))
        tasks.append(_run_poll(poll_arbeitnow, "Arbeitnow"))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_new = sum(r for r in results if isinstance(r, int))

    print(f"\n🚀 Ingestion complete! {total_new} brand-new jobs saved to Neon PostgreSQL.")
    return total_new


if __name__ == "__main__":
    asyncio.run(run_ingestion())
