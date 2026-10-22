import asyncio
import yaml
import httpx
from bs4 import BeautifulSoup
from src.db import get_connection, make_dedup_hash

with open("config/config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

PREFS = CONFIG["candidate_preferences"]

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    return BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)

def matches_basic_filters(title: str, location: str) -> bool:
    t_low = title.lower()
    l_low = (location or "").lower()

    if any(bad in t_low for bad in PREFS["exclude_title_keywords"]):
        return False
    if not any(good in t_low for good in PREFS["target_role_keywords"]):
        return False
    if not any(loc in l_low for loc in PREFS["allowed_locations"]):
        return False
    return True

def job_exists(conn, dedup_hash: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM jobs WHERE dedup_hash = %s", (dedup_hash,))
        return cur.fetchone() is not None

def insert_job(conn, source_ats: str, company: str, title: str, location: str, apply_url: str, jd_text: str) -> bool:
    dedup_hash = make_dedup_hash(company, title, location)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (dedup_hash, source_ats, company, title, location, apply_url, jd_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dedup_hash) DO NOTHING
            RETURNING id;
            """,
            (dedup_hash, source_ats, company, title, location, apply_url, jd_text),
        )
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted

async def poll_greenhouse(client: httpx.AsyncClient, conn, board: str) -> int:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    resp = await client.get(url)
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
    resp = await client.get(url)
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
    resp = await client.get(url)
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
    payload = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "AI OR Machine Learning OR Python OR Backend"}
    
    try:
        resp = await client.post(list_url, json=payload, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return 0
    except Exception:
        return 0

    added = 0
    for item in resp.json().get("jobPostings", []):
        title = item.get("title", "")
        loc = item.get("locationsText", "")
        ext_path = item.get("externalPath", "")
        if not ext_path or not matches_basic_filters(title, loc):
            continue

        # Check dedup BEFORE firing a second HTTP request for full JD
        if job_exists(conn, make_dedup_hash(company, title, loc)):
            continue

        detail_resp = await client.get(f"{base}{ext_path}", headers={"Accept": "application/json"})
        if detail_resp.status_code == 200:
            info = detail_resp.json().get("jobPostingInfo", {})
            jd = clean_html(info.get("jobDescription", ""))
            apply_url = info.get("externalUrl") or f"https://{cfg['tenant']}.{cfg['wd_instance']}.myworkdayjobs.com/{cfg['site']}{ext_path}"
            if insert_job(conn, "workday", company, title, loc, apply_url, jd):
                added += 1
    return added

async def run_ingestion():
    targets = CONFIG["ats_targets"]
    total_new = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/1.0)"}

    with get_connection() as conn:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            for b in targets.get("greenhouse", []):
                n = await poll_greenhouse(client, conn, b)
                print(f"  [Greenhouse] {b}: +{n} new matching jobs")
                total_new += n

            for s in targets.get("lever", []):
                n = await poll_lever(client, conn, s)
                print(f"  [Lever]      {s}: +{n} new matching jobs")
                total_new += n

            for a in targets.get("ashby", []):
                n = await poll_ashby(client, conn, a)
                print(f"  [Ashby]      {a}: +{n} new matching jobs")
                total_new += n

            for w in targets.get("workday_cxs", []):
                n = await poll_workday(client, conn, w)
                print(f"  [Workday]    {w['company']}: +{n} new matching jobs")
                total_new += n

    print(f"\n🚀 Ingestion complete! {total_new} brand-new jobs saved to Neon PostgreSQL.")

if __name__ == "__main__":
    asyncio.run(run_ingestion())