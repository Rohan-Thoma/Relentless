from difflib import SequenceMatcher
import logging
import os
import threading

logger = logging.getLogger(__name__)


class IngestionCache:
    def __init__(self):
        self._lock = threading.Lock()
        self.hydrated: bool = False
        self.seen_hashes: set[str] = set()
        self.company_sorted_titles: dict[str, set[str]] = {}
        self.processed_emails: set[str] = set()
        self.discovered_boards: set[tuple[str, str]] = set()

    def reset(self) -> None:
        with self._lock:
            self.hydrated = False
            self.seen_hashes.clear()
            self.company_sorted_titles.clear()
            self.processed_emails.clear()
            self.discovered_boards.clear()

    def hydrate_from_db(self) -> None:
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
            return

        try:
            from src.db import get_connection

            with get_connection() as conn, conn.cursor() as cur:
                # 1. Warm jobs
                cur.execute("SELECT dedup_hash, company, canonical_title FROM jobs;")
                job_rows = cur.fetchall()

                # 2. Warm processed emails
                cur.execute("SELECT message_id FROM processed_emails;")
                email_rows = cur.fetchall()

                # 3. Warm discovered boards
                cur.execute("SELECT platform, slug FROM discovered_ats_boards;")
                board_rows = cur.fetchall()

            with self._lock:
                self.seen_hashes.clear()
                self.company_sorted_titles.clear()
                for row in job_rows:
                    dh = row.get("dedup_hash") if isinstance(row, dict) else row[0]
                    comp = row.get("company") if isinstance(row, dict) else row[1]
                    canon = row.get("canonical_title") if isinstance(row, dict) else row[2]
                    if dh:
                        self.seen_hashes.add(str(dh))
                    if comp and canon:
                        ck = str(comp).strip().lower()
                        st = " ".join(sorted(str(canon).split()))
                        self.company_sorted_titles.setdefault(ck, set()).add(st)

                self.processed_emails.clear()
                for row in email_rows:
                    mid = row.get("message_id") if isinstance(row, dict) else row[0]
                    if mid:
                        self.processed_emails.add(str(mid))

                self.discovered_boards.clear()
                for row in board_rows:
                    plat = row.get("platform") if isinstance(row, dict) else row[0]
                    slug = row.get("slug") if isinstance(row, dict) else row[1]
                    if plat and slug:
                        self.discovered_boards.add((str(plat), str(slug)))

                self.hydrated = True
                logger.info(
                    "IngestionCache hydrated: %d jobs, %d emails, %d boards",
                    len(self.seen_hashes),
                    len(self.processed_emails),
                    len(self.discovered_boards),
                )
        except Exception as e:
            logger.warning("Failed to hydrate IngestionCache from DB: %s", e)

    def has_hash(self, h: str) -> bool:
        with self._lock:
            return h in self.seen_hashes

    def is_near_dup_cached(self, company: str, sorted_norm: str) -> bool:
        if not company or not sorted_norm:
            return False
        comp_key = company.strip().lower()
        with self._lock:
            candidates = list(self.company_sorted_titles.get(comp_key, ()))
        for existing in candidates:
            if SequenceMatcher(None, sorted_norm, existing).ratio() >= 0.92:
                return True
        return False

    def record_job(self, h: str, company: str, canonical: str) -> None:
        with self._lock:
            if h:
                self.seen_hashes.add(h)
            if company and canonical:
                ck = company.strip().lower()
                st = " ".join(sorted(str(canonical).split()))
                self.company_sorted_titles.setdefault(ck, set()).add(st)

    def has_email(self, mid: str) -> bool:
        with self._lock:
            return mid in self.processed_emails

    def record_email(self, mid: str) -> None:
        with self._lock:
            if mid:
                self.processed_emails.add(mid)

    def has_board(self, platform: str, slug: str) -> bool:
        with self._lock:
            return (platform, slug) in self.discovered_boards

    def record_board(self, platform: str, slug: str) -> None:
        with self._lock:
            if platform and slug:
                self.discovered_boards.add((platform, slug))


CACHE = IngestionCache()

