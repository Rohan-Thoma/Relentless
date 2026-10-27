from datetime import date, datetime, UTC
import re
from typing import Any


def parse_relative_age_hours(text: str | None) -> float | None:
    if not text:
        return None
    s = text.strip().lower()
    if not s:
        return None

    if "just now" in s or "moments ago" in s or "posted today" in s or s == "today":
        return 1.0
    if "yesterday" in s:
        return 24.0

    m = re.search(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month|year)s?", s)
    if not m:
        return None

    qty = float(m.group(1))
    unit = m.group(2)

    if unit in ("minute", "min"):
        return qty / 60.0
    if unit in ("hour", "hr"):
        return qty
    if unit in ("day",):
        return qty * 24.0
    if unit in ("week",):
        return qty * 24.0 * 7.0
    if unit in ("month",):
        return qty * 24.0 * 30.0
    if unit in ("year",):
        return qty * 24.0 * 365.0
    return None


def is_timestamp_fresh(val: Any, max_hours: float) -> bool:
    if max_hours <= 0:
        return True
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True

    now = datetime.now(UTC)

    if isinstance(val, (int, float)):
        # Lever uses milliseconds (13 digits), others use seconds
        dt = datetime.fromtimestamp(val / 1000.0 if val > 1e11 else val, tz=UTC)
        age_hours = (now - dt).total_seconds() / 3600.0
        return age_hours <= max_hours

    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=UTC)
        age_hours = (now - dt).total_seconds() / 3600.0
        return age_hours <= max_hours

    if isinstance(val, date):
        dt = datetime.combine(val, datetime.min.time(), tzinfo=UTC)
        age_hours = (now - dt).total_seconds() / 3600.0
        # +24h buffer for date-only granularity
        return (age_hours - 24.0) <= max_hours

    if isinstance(val, str):
        s = val.strip()
        rel = parse_relative_age_hours(s)
        if rel is not None:
            return rel <= max_hours

        try:
            clean_s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            age_hours = (now - dt).total_seconds() / 3600.0
            return age_hours <= max_hours
        except Exception:
            pass

        try:
            num = float(s)
            return is_timestamp_fresh(num, max_hours)
        except Exception:
            pass

        return True

    return True
