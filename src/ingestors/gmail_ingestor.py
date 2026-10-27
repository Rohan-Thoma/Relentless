import asyncio
from datetime import UTC, datetime, timedelta
import email
from email.header import decode_header
import email.policy
import email.utils
import hashlib
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
from src.ingestors.freshness import is_timestamp_fresh
from src.ingestors.url_resolver import register_discovered_board, resolve_direct_apply_url

logger = logging.getLogger(__name__)


def _decode_header_str(val: str) -> str:
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

    if conn is None:
        try:
            from src.ingestors.dedup_cache import CACHE

            if CACHE.hydrated and not os.getenv("PYTEST_CURRENT_TEST"):
                if CACHE.has_email(message_id):
                    return True
        except Exception:
            pass

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
            if inserted:
                try:
                    from src.ingestors.dedup_cache import CACHE

                    CACHE.record_email(message_id)
                except Exception:
                    pass
            return inserted

    if conn is not None:
        try:
            return _execute(conn)
        except Exception as e:
            logger.warning("Could not mark email processed: %s", e)
            return False

    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        try:
            from src.ingestors.dedup_cache import CACHE

            CACHE.record_email(message_id)
        except Exception:
            pass
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
    linkedin_ids = []
    direct_urls = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.startswith("http"):
            continue

        li_match = re.search(r"linkedin\.com/comm/jobs/view/(\d+)", href)
        if not li_match:
            li_match = re.search(r"linkedin\.com/jobs/view/(\d+)", href)
        if not li_match:
            li_match = re.search(r"currentJobId=(\d+)", href)

        if li_match:
            jid = li_match.group(1)
            if jid not in linkedin_ids:
                linkedin_ids.append(jid)
            continue

        ats_match = re.search(
            r"https?://(?:[a-zA-Z0-9_\-]+\.)*(?:greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|recruitee\.com)/[^\s\"'>]+",
            href,
        )
        if ats_match:
            direct = ats_match.group(0).split("?")[0]
            if direct not in direct_urls:
                direct_urls.append(direct)

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

        # 6. Posted time ago
        posted_el = soup.select_one(".posted-time-ago__text, time")
        posted_ago = posted_el.get_text(strip=True) if posted_el else None

        return {
            "job_id": linkedin_job_id,
            "title": title,
            "company": company,
            "location": location,
            "jd_text": jd_text,
            "external_apply_url": external_url,
            "posted_ago": posted_ago,
        }
    except Exception as e:
        logger.warning("Error fetching LinkedIn guest job %s: %s", linkedin_job_id, e)
        return None


def _find_imap_folder(mail: imaplib.IMAP4_SSL, target_label: str) -> str | None:
    """Finds exact IMAP folder name matching label, handling nested folder slashes/hyphens."""
    try:
        status, folders = mail.list()
        if status != "OK" or not folders:
            return None
        clean = target_label.strip().strip('"').lower()
        variants = {clean, clean.replace("-", "/"), clean.replace("/", "-")}
        for f_entry in folders:
            entry_str = f_entry.decode("utf-8", errors="replace") if isinstance(f_entry, bytes) else str(f_entry)
            m = re.search(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]+)"\s+(?P<name>.*)', entry_str)
            if m:
                fn = m.group("name").strip().strip('"')
                fn_low = fn.lower()
                if fn_low in variants:
                    return fn
                for v in variants:
                    if fn_low.endswith(("/" + v, "-" + v)):
                        return fn
    except Exception as e:
        logger.debug("Error listing IMAP folders: %s", e)
    return None


def fetch_alert_emails_sync(
    gmail_address: str,
    gmail_password: str,
    lookback_days: int = 3,
    sender_keywords: list[str] | None = None,
    gmail_label: str | None = None,
) -> list[dict]:
    keywords = [k.lower() for k in (sender_keywords or [])]
    emails = []

    cutoff_dt = datetime.now(UTC) - timedelta(days=lookback_days)
    since_date = cutoff_dt.strftime("%d-%b-%Y")

    if gmail_label is None:
        try:
            cfg = load_config()
            scanner_cfg = getattr(cfg, "gmail_scanner", None)
            gmail_label = os.getenv("GMAIL_LABEL", "").strip() or (
                scanner_cfg.label if scanner_cfg else "professional-job_alerts"
            )
        except Exception:
            gmail_label = "professional-job_alerts"

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(gmail_address, gmail_password)

        uids = []
        is_labeled_search = False

        # 1. Search by user's dedicated job alerts label with lookback constraint
        if gmail_label:
            try:
                status_box, _ = mail.select('"[Gmail]/All Mail"', readonly=True)
                if status_box != "OK":
                    status_box, _ = mail.select("[Gmail]/All Mail", readonly=True)
                if status_box != "OK":
                    mail.select("INBOX", readonly=True)

                label_candidates = [gmail_label]
                if "-" in gmail_label:
                    label_candidates.append(gmail_label.replace("-", "/"))

                for lc in label_candidates:
                    raw_query = f"label:{lc} newer_than:{lookback_days}d"
                    # In Gmail IMAP, the X-GM-RAW query argument MUST be enclosed in quotes
                    status, data = mail.uid("search", None, "X-GM-RAW", f'"{raw_query}"')
                    if status == "OK" and data and data[0]:
                        uids = data[0].split()
                        is_labeled_search = True
                        break
            except Exception as e:
                logger.debug("Gmail X-GM-RAW label search failed: %s", e)

            # Try selecting the label folder directly with SINCE date
            if not uids:
                try:
                    matched_folder = _find_imap_folder(mail, gmail_label) or gmail_label
                    status_box, _ = mail.select(f'"{matched_folder}"', readonly=True)
                    if status_box != "OK":
                        status_box, _ = mail.select(matched_folder, readonly=True)
                    if status_box == "OK":
                        status, data = mail.uid("search", None, f'(SINCE "{since_date}")')
                        if status == "OK" and data and data[0]:
                            uids = data[0].split()
                            is_labeled_search = True
                except Exception as e:
                    logger.debug("Selecting mailbox for label '%s' failed: %s", gmail_label, e)

            # Strict configuration enforcement: If a label is configured, NEVER search inbox
            if not uids:
                logger.info(
                    "Gmail Scanner: 0 emails found matching label '%s' in the last %d days.",
                    gmail_label,
                    lookback_days,
                )
                try:
                    mail.logout()
                except Exception:
                    pass
                return []

        # 2. Only if NO label was configured in settings, fall back to INBOX SINCE search
        else:
            mail.select("INBOX", readonly=True)
            status, data = mail.uid("search", None, f'(SINCE "{since_date}")')
            if status == "OK" and data and data[0]:
                uids = data[0].split()

        if not uids:
            try:
                mail.logout()
            except Exception:
                pass
            return []

        # Stage A: Batch-fetch headers in chunks of 100 UIDs
        candidate_uids = []
        uids_reversed = list(reversed(uids))

        for i in range(0, len(uids_reversed), 100):
            chunk = uids_reversed[i : i + 100]
            uid_set_str = b",".join(chunk).decode("ascii")
            status, fetch_data = mail.uid(
                "fetch", uid_set_str, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM DATE)])"
            )
            if status != "OK" or not fetch_data:
                continue

            for item in fetch_data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                header_bytes = item[1]
                hdr_msg = email.message_from_bytes(header_bytes, policy=email.policy.default)

                subject = _decode_header_str(hdr_msg.get("Subject", ""))
                sender = _decode_header_str(hdr_msg.get("From", ""))
                date_hdr = hdr_msg.get("Date", "")
                message_id = (hdr_msg.get("Message-ID") or "").strip()

                if not message_id:
                    message_id = hashlib.sha256(f"{sender}|{subject}|{date_hdr}".encode()).hexdigest()

                if date_hdr:
                    try:
                        parsed_dt = email.utils.parsedate_to_datetime(date_hdr)
                        if parsed_dt.tzinfo is None:
                            parsed_dt = parsed_dt.replace(tzinfo=UTC)
                        if parsed_dt < (cutoff_dt - timedelta(hours=12)):
                            continue
                    except Exception:
                        pass

                if not is_labeled_search and keywords:
                    combined = f"{sender} {subject}".lower()
                    if not any(kw in combined for kw in keywords):
                        continue

                if is_email_processed(message_id):
                    continue

                # Extract UID from header response
                uid_match = re.search(rb"UID (\d+)", item[0])
                msg_uid = uid_match.group(1) if uid_match else None
                if msg_uid:
                    candidate_uids.append((msg_uid, message_id, subject, sender))

        # Stage B: Fetch full body ONLY for candidate UIDs
        for msg_uid, message_id, subject, sender in candidate_uids:
            status, body_data = mail.uid("fetch", msg_uid, "(BODY.PEEK[])")
            if status != "OK" or not body_data:
                continue

            raw_email = None
            for part in body_data:
                if isinstance(part, tuple) and len(part) > 1:
                    raw_email = part[1]
                    break

            if not raw_email:
                continue

            msg = email.message_from_bytes(raw_email, policy=email.policy.default)

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

        try:
            mail.logout()
        except Exception:
            pass
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
        print("  📬 [Gmail Scanner] GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set. Skipping.", flush=True)
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

    max_age_hours = getattr(scanner_cfg, "max_job_age_hours", 96) if scanner_cfg else 96

    print(
        f"  📬 [Gmail Scanner] Checking mailbox '{gmail_address}' for label '{label}' (lookback: {days} days)...",
        flush=True,
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
        print("  📬 [Gmail Scanner] 0 new/unprocessed alert emails found.", flush=True)
        return 0

    print(f"  📬 [Gmail Scanner] Found {len(emails)} alert emails to process.", flush=True)
    total_extracted = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RelentlessJobScout/1.0)"}

    li_sem = asyncio.Semaphore(4)

    async with httpx.AsyncClient(timeout=15.0, headers=headers, follow_redirects=True) as client:
        for idx, mail in enumerate(emails, 1):
            msg_id = mail["message_id"]
            subject = mail["subject"]
            sender = mail["sender"]
            html = mail["html"]

            extracted_links = extract_job_links_from_email_html(html)
            li_ids = extracted_links.get("linkedin_job_ids", [])
            ats_urls = extracted_links.get("direct_ats_urls", [])
            print(
                f"    ↳ [Email {idx}/{len(emails)}] '{subject[:45]}' (From: {sender[:30]}) -> {len(li_ids)} job links, {len(ats_urls)} direct ATS links",
                flush=True,
            )
            mail_extracted_jobs = 0

            # 1. Register any direct ATS boards found in the email
            for ats_url in ats_urls:
                await asyncio.to_thread(register_discovered_board, ats_url)

            # 2. Fetch LinkedIn guest jobs concurrently with semaphore
            async def _process_li_id(li_id: str) -> bool:
                async with li_sem:
                    job_data = await fetch_linkedin_guest_job(client, li_id)
                if not job_data:
                    return False

                if not is_timestamp_fresh(job_data.get("posted_ago"), max_age_hours):
                    logger.debug("Skipping stale LinkedIn job %s (posted: %s)", li_id, job_data.get("posted_ago"))
                    return False

                title = job_data["title"]
                company = job_data["company"]
                location = job_data["location"]
                jd_text = job_data["jd_text"]
                external_apply = job_data.get("external_apply_url")

                linkedin_fallback = f"https://www.linkedin.com/jobs/view/{li_id}"
                apply_url = external_apply or linkedin_fallback

                if "linkedin.com" in apply_url.lower():
                    apply_url = await resolve_direct_apply_url(client, company, title, fallback_url=apply_url)

                if matches_basic_filters(title, location):
                    inserted = await asyncio.to_thread(
                        insert_job,
                        None,
                        "gmail_alert",
                        company,
                        title,
                        location,
                        apply_url,
                        jd_text,
                        original_source_url=linkedin_fallback,
                    )
                    return bool(inserted)
                return False

            results = await asyncio.gather(*[_process_li_id(lid) for lid in li_ids])
            mail_extracted_jobs = sum(1 for r in results if r)
            total_extracted += mail_extracted_jobs

            await asyncio.to_thread(mark_email_processed, msg_id, subject, sender, mail_extracted_jobs)
            print(f"      + Extracted {mail_extracted_jobs} new matching jobs from this email.", flush=True)
            if mail_extracted_jobs > 0:
                logger.info(
                    "Gmail Alert [%s]: Extracted +%d jobs from email '%s'",
                    sender,
                    mail_extracted_jobs,
                    subject,
                )

    print(
        f"  ✅ [Gmail Scanner Summary] Processed {len(emails)} emails -> +{total_extracted} total new jobs saved.",
        flush=True,
    )
    return total_extracted


if __name__ == "__main__":
    asyncio.run(poll_gmail_alerts())
