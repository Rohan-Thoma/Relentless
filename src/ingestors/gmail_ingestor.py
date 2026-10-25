import asyncio
from datetime import datetime, timedelta
import email
from email.header import decode_header
import email.policy
import imaplib
import logging
import os
import re
import urllib.parse

from bs4 import BeautifulSoup
import httpx

from src.config import load_config
from src.db import get_connection
from src.ingestors.common import clean_html, insert_job, matches_basic_filters
from src.ingestors.url_resolver import (
    DIRECT_ATS_DOMAINS,
    register_discovered_board,
    resolve_direct_apply_url,
)

logger = logging.getLogger(__name__)

LINKEDIN_JOB_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)"
)


def _decode_header_str(val: str | None) -> str:
    if not val:
        return ""
    decoded_parts = []
    for part, enc in decode_header(val):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    return " ".join(decoded_parts).strip()


def is_email_processed(message_id: str, conn=None) -> bool:
    if not message_id:
        return False

    def _query(c):
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_emails WHERE message_id = %s;", (message_id,))
            return cur.fetchone() is not None

    if conn is not None:
        try:
            return _query(conn)
        except Exception as e:
            logger.warning("Could not check processed_emails: %s", e)
            return False

    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return False

    try:
        with get_connection() as c:
            return _query(c)
    except Exception as e:
        logger.warning("Could not check processed_emails: %s", e)
        return False


def mark_email_processed(
    message_id: str, subject: str, sender: str, jobs_extracted: int = 0, conn=None
) -> bool:
    if not message_id:
        return False

    def _execute(c):
        with c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO processed_emails (message_id, subject, sender, jobs_extracted, processed_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id;
                """,
                (message_id, subject, sender, jobs_extracted),
            )
            inserted = cur.fetchone() is not None
            c.commit()
            return inserted

    if conn is not None:
        try:
            return _execute(conn)
        except Exception as e:
            logger.warning("Could not mark email processed: %s", e)
            return False

    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return True

    try:
        with get_connection() as c:
            return _execute(c)
    except Exception as e:
        logger.warning("Could not mark email processed: %s", e)
        return False


def extract_job_links_from_email_html(html: str) -> dict:
    if not html:
        return {"linkedin_job_ids": [], "direct_ats_urls": []}

    soup = BeautifulSoup(html, "html.parser")
    found_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href:
            found_urls.add(href)

    for text_url in re.findall(r"https?://[^\s\"'<>]+", html):
        found_urls.add(text_url)

    linkedin_ids = []
    direct_urls = []

    for url in found_urls:
        li_match = LINKEDIN_JOB_URL_PATTERN.search(url)
        if li_match:
            job_id = li_match.group(1)
            if job_id not in linkedin_ids:
                linkedin_ids.append(job_id)
        else:
            url_low = url.lower()
            if any(dom in url_low for dom in DIRECT_ATS_DOMAINS):
                clean_url = url.split("?")[0] if "?" in url else url
                if clean_url not in direct_urls:
                    direct_urls.append(clean_url)

    return {
        "linkedin_job_ids": linkedin_ids,
        "direct_ats_urls": direct_urls,
    }


async def fetch_linkedin_guest_job(
    client: httpx.AsyncClient, linkedin_job_id: str
) -> dict | None:
    if not linkedin_job_id:
        return None

    guest_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{linkedin_job_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        resp = await client.get(guest_url, headers=headers, timeout=12.0)
        if resp.status_code != 200:
            logger.debug(
                "LinkedIn guest API returned %d for job_id %s",
                resp.status_code,
                linkedin_job_id,
            )
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # 1. Job Title
        title_el = soup.select_one(".top-card-layout__title, h1, h2")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            return None

        # 2. Company Name
        comp_el = soup.select_one(".topcard__org-name-link, .topcard__flavor--black-link")
        if not comp_el:
            for el in soup.select(".topcard__flavor"):
                classes = el.get("class", [])
                if "topcard__flavor--bullet" not in classes:
                    comp_el = el
                    break
        company = comp_el.get_text(strip=True) if comp_el else "Unknown"

        # 3. Location
        loc_el = soup.select_one(".topcard__flavor--bullet")
        location = loc_el.get_text(strip=True) if loc_el else ""

        # 4. Job Description Text
        jd_el = soup.select_one(
            ".show-more-less-html__markup, .description__text, .decorated-job-posting__details"
        )
        jd_text = clean_html(str(jd_el)) if jd_el else ""

        # 5. External apply URL
        external_url = ""
        code_el = soup.select_one("code#applyUrl")
        if code_el:
            raw = code_el.get_text(strip=True).strip("<!- >\"'")
            if raw.startswith("http"):
                external_url = raw

        if not external_url:
            for btn in soup.select("a.apply-button, a[data-tracking-control-name*='apply']"):
                href = btn.get("href", "")
                if "url=" in href:
                    parsed = urllib.parse.urlparse(href)
                    qs = urllib.parse.parse_qs(parsed.query)
                    if qs.get("url"):
                        external_url = urllib.parse.unquote(qs["url"][0])
                        break
                elif href.startswith("http") and "linkedin.com" not in href:
                    external_url = href
                    break

        return {
            "job_id": linkedin_job_id,
            "title": title,
            "company": company,
            "location": location,
            "jd_text": jd_text,
            "external_apply_url": external_url,
        }
    except Exception as e:
        logger.warning("Error fetching LinkedIn guest job %s: %s", linkedin_job_id, e)
        return None


def fetch_alert_emails_sync(
    gmail_address: str,
    gmail_password: str,
    lookback_days: int = 3,
    sender_keywords: list[str] | None = None,
    gmail_label: str | None = "professional-job_alerts",
) -> list[dict]:
    keywords = [k.lower() for k in (sender_keywords or [])]
    emails = []

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_address, gmail_password)

        msg_ids = []
        is_labeled_search = False

        # 1. Search by user's dedicated job alerts label first (e.g., label:professional-job_alerts)
        if gmail_label:
            try:
                mail.select("INBOX", readonly=True)
                status, data = mail.search(None, "X-GM-RAW", f"label:{gmail_label}")
                if status == "OK" and data and data[0]:
                    msg_ids = data[0].split()
                    is_labeled_search = True
            except Exception as e:
                logger.debug("Gmail X-GM-RAW label search failed: %s", e)

            # Try selecting the label folder directly if X-GM-RAW did not yield
            if not msg_ids:
                try:
                    status_box, _ = mail.select(f'"{gmail_label}"', readonly=True)
                    if status_box == "OK":
                        status, data = mail.search(None, "ALL")
                        if status == "OK" and data and data[0]:
                            msg_ids = data[0].split()
                            is_labeled_search = True
                except Exception as e:
                    logger.debug("Selecting mailbox '%s' failed: %s", gmail_label, e)

        # 2. Fallback to INBOX SINCE search if no label search succeeded
        if not msg_ids:
            mail.select("INBOX", readonly=True)
            since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            status, data = mail.search(None, f'(SINCE "{since_date}")')
            if status == "OK" and data and data[0]:
                msg_ids = data[0].split()

        if not msg_ids:
            mail.logout()
            return []

        # Fetch newest first
        for msg_id_bytes in reversed(msg_ids):
            status, msg_data = mail.fetch(msg_id_bytes, "(RFC822)")
            if status != "OK" or not msg_data:
                continue

            raw_email = None
            for part in msg_data:
                if isinstance(part, tuple) and len(part) > 1:
                    raw_email = part[1]
                    break

            if not raw_email:
                continue

            msg = email.message_from_bytes(raw_email, policy=email.policy.default)
            message_id = (msg.get("Message-ID") or str(msg_id_bytes)).strip()
            subject = _decode_header_str(msg.get("Subject", ""))
            sender = _decode_header_str(msg.get("From", ""))

            # If not sourced from the dedicated label, filter by sender/subject keywords
            if not is_labeled_search and keywords:
                combined = f"{sender} {subject}".lower()
                if not any(kw in combined for kw in keywords):
                    continue

            if is_email_processed(message_id):
                continue

            # Extract HTML body
            html_content = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_content = payload.decode(
                                part.get_content_charset() or "utf-8", errors="replace"
                            )
                            break
                    elif ctype == "text/plain" and not html_content:
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_content = payload.decode(
                                part.get_content_charset() or "utf-8", errors="replace"
                            )
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    html_content = payload.decode(
                        msg.get_content_charset() or "utf-8", errors="replace"
                    )

            if html_content:
                emails.append({
                    "message_id": message_id,
                    "subject": subject,
                    "sender": sender,
                    "html": html_content,
                })

        mail.logout()
    except Exception as e:
        logger.warning("Error fetching emails from Gmail: %s", e)

    return emails


async def poll_gmail_alerts(
    lookback_days: int | None = None,
    sender_keywords: list[str] | None = None,
    gmail_label: str | None = None,
) -> int:
    gmail_address = os.getenv("GMAIL_ADDRESS", "").strip()
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()

    if not gmail_address or not gmail_password:
        logger.info("Gmail credentials (GMAIL_ADDRESS/GMAIL_APP_PASSWORD) not set. Skipping.")
        return 0

    cfg = load_config()
    scanner_cfg = getattr(cfg, "gmail_scanner", None)

    days = lookback_days
    if days is None:
        env_days = os.getenv("GMAIL_LOOKBACK_DAYS", "").strip()
        days = int(env_days) if env_days.isdigit() else (
            scanner_cfg.lookback_days if scanner_cfg else 3
        )

    keywords = sender_keywords
    if not keywords:
        keywords = scanner_cfg.sender_keywords if scanner_cfg else [
            "linkedin", "naukri", "indeed", "instahyre", "wellfound", "job", "hiring", "alert"
        ]

    label = gmail_label
    if label is None:
        label = os.getenv("GMAIL_LABEL", "").strip() or (
            scanner_cfg.label if scanner_cfg else "professional-job_alerts"
        )

    emails = await asyncio.to_thread(
        fetch_alert_emails_sync,
        gmail_address=gmail_address,
        gmail_password=gmail_password,
        lookback_days=days,
        sender_keywords=keywords,
        gmail_label=label,
    )

    if not emails:
        return 0

    total_extracted = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/1.0)"}

    async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
        for mail in emails:
            msg_id = mail["message_id"]
            subject = mail["subject"]
            sender = mail["sender"]
            html = mail["html"]

            extracted_links = extract_job_links_from_email_html(html)
            mail_extracted_jobs = 0

            # 1. Register any direct ATS boards found in the email
            for ats_url in extracted_links.get("direct_ats_urls", []):
                register_discovered_board(ats_url)

            # 2. Fetch full JD from LinkedIn guest API for each LinkedIn Job ID
            for li_id in extracted_links.get("linkedin_job_ids", []):
                job_data = await fetch_linkedin_guest_job(client, li_id)
                if not job_data:
                    continue

                title = job_data["title"]
                company = job_data["company"]
                location = job_data["location"]
                jd_text = job_data["jd_text"]
                external_apply = job_data.get("external_apply_url")

                linkedin_fallback = f"https://www.linkedin.com/jobs/view/{li_id}"
                apply_url = external_apply or linkedin_fallback

                # If apply_url still points to LinkedIn, attempt resolving the company's direct ATS
                if "linkedin.com" in apply_url.lower():
                    apply_url = await resolve_direct_apply_url(
                        client, company, title, fallback_url=apply_url
                    )

                if matches_basic_filters(title, location):
                    inserted = insert_job(
                        None,
                        "gmail_alert",
                        company,
                        title,
                        location,
                        apply_url,
                        jd_text,
                        original_source_url=linkedin_fallback,
                    )
                    if inserted:
                        mail_extracted_jobs += 1
                        total_extracted += 1

            mark_email_processed(msg_id, subject, sender, mail_extracted_jobs)
            if mail_extracted_jobs > 0:
                logger.info(
                    "Gmail Alert [%s]: Extracted +%d jobs from email '%s'",
                    sender,
                    mail_extracted_jobs,
                    subject,
                )

    return total_extracted


if __name__ == "__main__":
    asyncio.run(poll_gmail_alerts())

