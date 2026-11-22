"""Stateful polling watermarks & ETag tracking for incremental ingestion."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
import os

from src.db import get_connection

logger = logging.getLogger(__name__)


def _db_enabled() -> bool:
    return not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"))


def get_watermark(source_key: str) -> dict | None:
    if not source_key or not _db_enabled():
        return None
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT source_key, last_polled_at, last_etag, last_item_count FROM source_watermarks WHERE source_key = %s;",
                (source_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.debug("Could not fetch watermark for %s: %s", source_key, e)
        return None


def update_watermark(source_key: str, etag: str | None = None, item_count: int = 0) -> None:
    if not source_key or not _db_enabled():
        return
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_watermarks (source_key, last_polled_at, last_etag, last_item_count, updated_at)
                VALUES (%s, NOW(), %s, %s, NOW())
                ON CONFLICT (source_key) DO UPDATE SET
                    last_polled_at = NOW(),
                    last_etag = COALESCE(EXCLUDED.last_etag, source_watermarks.last_etag),
                    last_item_count = EXCLUDED.last_item_count,
                    updated_at = NOW();
                """,
                (source_key, etag, int(item_count)),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Could not update watermark for %s: %s", source_key, e)


def compute_delta_hours(
    source_key: str,
    default_hours: int = 72,
    overlap_hours: float = 2.0,
    min_hours: int = 2,
) -> int:
    """Computes incremental lookback hours since last successful poll (capped by default_hours)."""
    wm = get_watermark(source_key)
    if not wm or not wm.get("last_polled_at"):
        return int(default_hours)
    last_dt = wm["last_polled_at"]
    if isinstance(last_dt, str):
        try:
            last_dt = datetime.fromisoformat(last_dt.replace("Z", "+00:00"))
        except Exception:
            return int(default_hours)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=UTC)
    elapsed_hrs = max(0.0, (datetime.now(UTC) - last_dt).total_seconds() / 3600.0)
    delta = int(round(elapsed_hrs + overlap_hours))
    return max(min_hours, min(int(default_hours), delta))
