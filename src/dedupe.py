from difflib import SequenceMatcher
import re

# Only strip non-semantic filler tokens — NEVER strip seniority (senior/junior/lead/staff/i/ii/iii)
# or work mode (remote/hybrid/onsite), otherwise distinct roles at the same company collide!
NOISE_FILLER = {"full", "time", "fulltime", "urgent", "immediate", "hiring", "opening", "role", "position"}


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    tokens = [w for w in t.split() if w not in NOISE_FILLER]
    return " ".join(tokens)


def is_near_dup(conn, company: str, title: str, work_mode: str | None = None, window_days: int = 30) -> bool:
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

            with get_connection() as c:
                return is_near_dup(c, company, title, work_mode=work_mode, window_days=window_days)
        except Exception:
            return False

    try:
        with conn.cursor() as cur:
            if work_mode:
                cur.execute(
                    """
                    SELECT canonical_title, work_mode
                    FROM jobs
                    WHERE LOWER(company) = LOWER(%s)
                      AND discovered_at > NOW() - (%s || ' days')::interval;
                    """,
                    (company, int(window_days)),
                )
            else:
                cur.execute(
                    """
                    SELECT canonical_title
                    FROM jobs
                    WHERE LOWER(company) = LOWER(%s)
                      AND discovered_at > NOW() - (%s || ' days')::interval;
                    """,
                    (company, int(window_days)),
                )
            rows = cur.fetchall()
    except Exception:
        # Roll back aborted PostgreSQL transaction before running fallback query
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT canonical_title FROM jobs WHERE LOWER(company) = LOWER(%s);",
                    (company,),
                )
                rows = cur.fetchall()
        except Exception:
            if hasattr(conn, "rollback"):
                try:
                    conn.rollback()
                except Exception:
                    pass
            return False

    for row in rows:
        if isinstance(row, dict):
            existing = row.get("canonical_title")
            existing_wm = row.get("work_mode")
        else:
            existing = row[0] if len(row) > 0 else None
            existing_wm = row[1] if len(row) > 1 else None
        if not existing:
            continue
        # Never treat Remote vs On-site/Hybrid at the same company as a duplicate!
        if work_mode and existing_wm and work_mode.lower() != str(existing_wm).lower():
            continue
        sorted_existing = " ".join(sorted(str(existing).split()))
        if SequenceMatcher(None, sorted_norm, sorted_existing).ratio() >= 0.92:
            return True
    return False


def is_semantic_dup(
    conn,
    company: str,
    embedding: list[float],
    work_mode: str | None = None,
    threshold: float = 0.96,
    window_days: int = 30,
) -> bool:
    """Tier-3 pgvector HNSW cosine similarity duplicate check for reworded reposts."""
    if not conn or not company or not embedding:
        return False
    max_dist = max(0.0, 1.0 - float(threshold))
    vec_str = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    try:
        with conn.cursor() as cur:
            if work_mode:
                cur.execute(
                    """
                    SELECT 1
                    FROM jobs
                    WHERE LOWER(company) = LOWER(%s)
                      AND embedding IS NOT NULL
                      AND (work_mode IS NULL OR LOWER(work_mode) = LOWER(%s))
                      AND discovered_at > NOW() - (%s || ' days')::interval
                      AND (embedding <=> %s::vector) <= %s
                    LIMIT 1;
                    """,
                    (company, work_mode, int(window_days), vec_str, max_dist),
                )
            else:
                cur.execute(
                    """
                    SELECT 1
                    FROM jobs
                    WHERE LOWER(company) = LOWER(%s)
                      AND embedding IS NOT NULL
                      AND discovered_at > NOW() - (%s || ' days')::interval
                      AND (embedding <=> %s::vector) <= %s
                    LIMIT 1;
                    """,
                    (company, int(window_days), vec_str, max_dist),
                )
            return cur.fetchone() is not None
    except Exception:
        if hasattr(conn, "rollback"):
            try:
                conn.rollback()
            except Exception:
                pass
        return False
