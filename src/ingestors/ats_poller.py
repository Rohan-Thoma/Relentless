import asyncio
import re

import httpx
from bs4 import BeautifulSoup

from src.config import load_config
from src.db import get_connection, make_dedup_hash
from src.dedupe import normalize_title, is_near_dup
from src.httpx_utils import get_with_retry, post_with_retry


def _cfg():
    return load_config()


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)


def _tokenize(text: str):
    # word-boundary-ish tokens; keep technical tokens
    return re.findall(r"[a-z0-9+#\.]+", (text or "").lower())


def _norm_loc(loc: str) -> str:
    l = (loc or "").lower()
    aliases = {"bengaluru": "bangalore", "in": "india"}
    for k, v in aliases.items():
        l = l.replace(k, v)
    return l


def matches_basic_filters(title: str, location: str) -> bool:
    cfg = _cfg()
    prefs = cfg.candidate_preferences
    t_low = (title or "").lower()
    l_low = _norm_loc(location or "")

    # exclusion: word-boundary-ish (substring okay for short seniority markers)
    for bad in prefs.exclude_title_keywords:
        if bad.lower() in t_low.split() or bad.lower() in t_low:
            # require boundary-ish: bad as whole word
            if re.search(r"\b" + re.escape(bad.lower()) + r"\b", t_low):
                return False
    # keyword match: whole-word for plain words; substring for technical tokens
    tech_tokens = {"c++", "c#", ".net", "node.js", "ai", "ml"}
    matched = False
    for good in prefs.target_role_keywords:
        g = good.lower()
        if g in tech_tokens:
            if g in t_low:
                matched = True
                break
        else:
            if re.search(r"\b" + re.escape(g) + r"\b", t_low):
                matched = True
                break
    if not matched:
        return False
    # location match
    loc_ok = False
    for loc in prefs.allowed_locations:
        ll = loc.lower()
        if ll == "remote":
            if "remote" in l_low or "hybrid" in l_low:
                loc_ok = True
                break
        elif ll in l_low:
            loc_ok = True
            break
    if not loc_ok:
        return False
    return True

def job_exists(conn, dedup_hash: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE dedup_hash = %s", (dedup_hash,))
        return cur.fetchone() is not None

def insert_job(conn, source_ats: str, company: str, title: str, location: str, apply_url: str, jd_text: str) -> bool:
    dedup_hash = make_dedup_hash(company, title, location)
    if is_near_dup(conn, company, title):
        return False
    canonical = normalize_title(title)
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
        loc = job.get("location", {}).get("name", "")
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
        loc = job.get("categories", {}).get("location", "")
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
        loc = job.get("location", "")
        if job.get("isRemote"):
            loc = f"{loc} (Remote)"
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
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": "AI OR Machine Learning OR Python OR Backend"}
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
            if job_exists(conn, make_dedup_hash(company, title, loc)) or is_near_dup(conn, company, title):
                continue
            detail_resp = await get_with_retry(client, f"{base}{ext_path}", headers={"Accept": "application/json"})
            if detail_resp.status_code == 200:
                info = detail_resp.json().get("jobPostingInfo", {})
                jd = clean_html(info.get("jobDescription", ""))
                apply_url = info.get("externalUrl") or f"https://{cfg['tenant']}.{cfg['wd_instance']}.myworkdayjobs.com/{cfg['site']}{ext_path}"
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
    for job in resp.json().get("content", []):
        title = job.get("name", "")
        loc = job.get("location", {}).get("city", "") if isinstance(job.get("location"), dict) else ""
        if matches_basic_filters(title, loc):
            jd = job.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "") if isinstance(job.get("jobAd"), dict) else ""
            apply_url = job.get("applyUrl", "")
            if insert_job(conn, "smartrecruiters", company, title, loc, apply_url, clean_html(jd)):
                added += 1
    return added


async def poll_recruitee(client: httpx.AsyncClient, conn, company: str) -> int:
    url = f"https://{company}.recruitee.com/api/offers/"
    resp = await get_with_retry(client, url)
    if resp.status_code != 200:
        return 0
    added = 0
    for job in resp.json().get("offers", []):
        title = job.get("title", "")
        loc = job.get("location", "")
        if matches_basic_filters(title, loc):
            jd = job.get("description", "")
            apply_url = job.get("careers_url", "") or job.get("url", "")
            if insert_job(conn, "recruitee", company, title, loc, apply_url, clean_html(jd)):
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
    total_new = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/1.0)"}

    with get_connection() as conn:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            for b in targets.get("greenhouse", []):
                try:
                    n = await poll_greenhouse(client, conn, b)
                except Exception as e:
                    print(f"  [Greenhouse] {b}: error {e}")
                    n = 0
                print(f"  [Greenhouse] {b}: +{n} new matching jobs")
                total_new += n

            for s in targets.get("lever", []):
                try:
                    n = await poll_lever(client, conn, s)
                except Exception as e:
                    print(f"  [Lever]      {s}: error {e}")
                    n = 0
                print(f"  [Lever]      {s}: +{n} new matching jobs")
                total_new += n

            for a in targets.get("ashby", []):
                try:
                    n = await poll_ashby(client, conn, a)
                except Exception as e:
                    print(f"  [Ashby]      {a}: error {e}")
                    n = 0
                print(f"  [Ashby]      {a}: +{n} new matching jobs")
                total_new += n

            for w in targets.get("workday_cxs", []):
                try:
                    n = await poll_workday(client, conn, w)
                except Exception as e:
                    print(f"  [Workday]    {w['company']}: error {e}")
                    n = 0
                print(f"  [Workday]    {w['company']}: +{n} new matching jobs")
                total_new += n

            for s in targets.get("smartrecruiters", []):
                try:
                    n = await poll_smartrecruiters(client, conn, s)
                except Exception as e:
                    print(f"  [SmartRecruiters] {s}: error {e}")
                    n = 0
                print(f"  [SmartRecruiters] {s}: +{n} new matching jobs")
                total_new += n

            for r in targets.get("recruitee", []):
                try:
                    n = await poll_recruitee(client, conn, r)
                except Exception as e:
                    print(f"  [Recruitee]  {r}: error {e}")
                    n = 0
                print(f"  [Recruitee]  {r}: +{n} new matching jobs")
                total_new += n

    print(f"\n🚀 Ingestion complete! {total_new} brand-new jobs saved to Neon PostgreSQL.")

if __name__ == "__main__":
    asyncio.run(run_ingestion())