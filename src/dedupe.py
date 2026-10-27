import re
from difflib import SequenceMatcher

FILLER = {"staff", "senior", "jr", "ii", "i", "remote", "hybrid", "junior", "lead", "principal", "iii"}


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    tokens = [w for w in t.split() if w not in FILLER]
    return " ".join(tokens)


def is_near_dup(conn, company: str, title: str) -> bool:
    norm = normalize_title(title)
    if not norm:
        return False
    sorted_norm = " ".join(sorted(norm.split()))

    if conn is None:
        try:
            from src.ingestors.dedup_cache import CACHE

            if CACHE.hydrated:
                return CACHE.is_near_dup_cached(company, sorted_norm)
        except Exception:
            pass

        try:
            from src.db import get_connection

            with get_connection() as c, c.cursor() as cur:
                cur.execute("SELECT canonical_title FROM jobs WHERE LOWER(company) = LOWER(%s);", (company,))
                rows = cur.fetchall()
        except Exception:
            return False
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT canonical_title FROM jobs WHERE LOWER(company) = LOWER(%s);", (company,))
            rows = cur.fetchall()
    for row in rows:
        existing = row.get("canonical_title") if isinstance(row, dict) else row[0]
        if not existing:
            continue
        sorted_existing = " ".join(sorted(str(existing).split()))
        if SequenceMatcher(None, sorted_norm, sorted_existing).ratio() >= 0.92:
            return True
    return False
