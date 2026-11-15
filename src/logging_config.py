"""
Structured logging and secret-redacting in-memory ring buffer for Relentless.

Provides:
- Idempotent `configure_logging()` for terminal + Web UI `/api/logs` + Telegram `/logs`
- Automatic secret redaction (API keys, Bearer tokens, Telegram tokens, DB passwords)
- Thread-safe ring buffer queryable by level, substring search, and monotonic `since_id`
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

MAX_LOG_ENTRIES = 1000

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)([^@\s]+)(@)"), r"\1***\3"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{12,}\b"), "sk-or-v1-***"),
    (re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b"), "sk-***"),
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE), r"\1***"),
    (re.compile(r"(https?://api\.telegram\.org/bot)\d+:[A-Za-z0-9_-]+"), r"\1<BOT_TOKEN>"),
    (re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{25,}\b"), "<TELEGRAM_TOKEN>"),
]

_LEVEL_MAP = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}

_lock = threading.RLock()
_ring: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_ENTRIES)
_next_id = 0
_tls = threading.local()
_configured = False
_stdio_wrapped = False


def redact_secrets(text: str) -> str:
    """Masks common API keys, Bearer tokens, bot tokens, and DB passwords."""
    if not text:
        return ""
    masked = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        masked = pattern.sub(repl, masked)
    return masked


def _infer_level_from_text(line: str, default_level: str = "INFO") -> str:
    upper = line.upper()
    if "❌" in line or "FATAL" in upper or "TRACEBACK (MOST RECENT CALL LAST)" in upper:
        return "ERROR"
    if "🚨" in line or "WARNING" in upper or "⚠️" in line:
        return "WARNING"
    return default_level


def _append_log_entry(level: str, logger_name: str, message: str) -> dict[str, Any] | None:
    clean = redact_secrets(message.rstrip("\r\n"))
    if not clean.strip():
        return None
    global _next_id
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        _next_id += 1
        entry = {
            "id": _next_id,
            "ts": ts,
            "level": level.upper(),
            "logger": logger_name or "relentless",
            "message": clean,
        }
        _ring.append(entry)
        return entry


class _RingBufferHandler(logging.Handler):
    """Stores formatted Python logging records in the bounded memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                formatter = self.formatter or logging.Formatter()
                tb = formatter.formatException(record.exc_info)
                msg = f"{msg}\n{tb}" if msg else tb
            _append_log_entry(record.levelname, record.name, msg)
        except Exception:
            self.handleError(record)


class _SafeConsoleHandler(logging.StreamHandler):
    """StreamHandler guarded so stdout/stderr teeing never re-records logger lines."""

    def emit(self, record: logging.LogRecord) -> None:
        prev = getattr(_tls, "emitting", False)
        _tls.emitting = True
        try:
            super().emit(record)
        finally:
            _tls.emitting = prev


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"
        payload = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(msg),
        }
        return json.dumps(payload, ensure_ascii=False)


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        msg = record.getMessage()
        if record.exc_info:
            msg = f"{msg}\n{self.formatException(record.exc_info)}"
        return redact_secrets(f"[{ts}] {record.levelname:<7} {record.name}: {msg}")


class _LogTeeStream:
    """Tees stdout/stderr prints to the original stream and the in-memory ring buffer."""

    def __init__(self, stream: Any, logger_name: str, default_level: str) -> None:
        self._stream = stream
        self._logger_name = logger_name
        self._default_level = default_level
        self._buf = ""

    def write(self, data: str) -> int:
        res = self._stream.write(data)
        if not data or getattr(_tls, "emitting", False):
            return res
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.strip()
            if stripped:
                inferred = _infer_level_from_text(stripped, self._default_level)
                _append_log_entry(inferred, self._logger_name, stripped)
        return res

    def flush(self) -> None:
        if self._buf.strip() and not getattr(_tls, "emitting", False):
            stripped = self._buf.strip()
            self._buf = ""
            inferred = _infer_level_from_text(stripped, self._default_level)
            _append_log_entry(inferred, self._logger_name, stripped)
        self._stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def configure_logging(
    level: str | None = None,
    json_logs: bool | None = None,
    capture_stdio: bool = True,
) -> logging.Logger:
    """Initializes console logging and the in-memory ring buffer idempotently."""
    global _configured, _stdio_wrapped

    resolved_level_name = (level or os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    resolved_level = _LEVEL_MAP.get(resolved_level_name, logging.INFO)
    use_json = (
        json_logs
        if json_logs is not None
        else os.getenv("LOG_FORMAT", "").strip().lower() == "json"
    )

    root = logging.getLogger()
    root.setLevel(resolved_level)

    if not any(isinstance(h, _RingBufferHandler) for h in root.handlers):
        ring_handler = _RingBufferHandler()
        ring_handler.setLevel(logging.DEBUG)
        root.addHandler(ring_handler)

    if not any(isinstance(h, _SafeConsoleHandler) for h in root.handlers):
        # Avoid duplicate console handlers if basicConfig ran earlier
        for existing in list(root.handlers):
            if isinstance(existing, logging.StreamHandler) and not isinstance(
                existing, (_RingBufferHandler, _SafeConsoleHandler)
            ):
                root.removeHandler(existing)
        console_handler = _SafeConsoleHandler(sys.stdout)
        console_handler.setLevel(resolved_level)
        console_handler.setFormatter(_JsonFormatter() if use_json else _HumanFormatter())
        root.addHandler(console_handler)

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if capture_stdio and not _stdio_wrapped and "pytest" not in sys.modules:
        if not isinstance(sys.stdout, _LogTeeStream):
            sys.stdout = _LogTeeStream(sys.stdout, "stdout", "INFO")
        if not isinstance(sys.stderr, _LogTeeStream):
            sys.stderr = _LogTeeStream(sys.stderr, "stderr", "ERROR")
        _stdio_wrapped = True

    _configured = True
    return root


def get_recent_logs(
    limit: int = 100,
    level: str | None = None,
    query: str | None = None,
    since_id: int = 0,
) -> dict[str, Any]:
    """Returns ring-buffered logs ordered oldest -> newest with level counts."""
    bounded_limit = max(1, min(int(limit or 100), MAX_LOG_ENTRIES))
    level_filter = level.strip().upper() if level and level.strip() else None
    if level_filter == "ALL":
        level_filter = None
    query_filter = query.strip().lower() if query and query.strip() else None

    with _lock:
        snapshot = list(_ring)
        latest_id = _next_id

    counts_by_level = {"ERROR": 0, "WARNING": 0, "INFO": 0, "DEBUG": 0}
    filtered: list[dict[str, Any]] = []
    for item in snapshot:
        lvl = item["level"]
        counts_by_level[lvl] = counts_by_level.get(lvl, 0) + 1
        if since_id and item["id"] <= since_id:
            continue
        if level_filter and lvl != level_filter:
            continue
        if query_filter:
            haystack = f"{item['level']} {item['logger']} {item['message']}".lower()
            if query_filter not in haystack:
                continue
        filtered.append(item)

    entries = filtered[-bounded_limit:]
    return {
        "entries": entries,
        "latest_id": latest_id,
        "total_buffered": len(snapshot),
        "counts_by_level": counts_by_level,
    }


def clear_log_buffer() -> int:
    """Clears all buffered in-memory log entries and returns the cleared count."""
    with _lock:
        count = len(_ring)
        _ring.clear()
        return count


def format_logs_text(
    limit: int = 25,
    level: str | None = None,
    query: str | None = None,
) -> str:
    """Formats buffered log entries cleanly for plain-text Telegram `/logs` or downloads."""
    res = get_recent_logs(limit=limit, level=level, query=query)
    entries = res["entries"]
    if not entries:
        return "No buffered logs matched your filter yet."
    lines = []
    for e in entries:
        short_ts = e["ts"][11:19] if len(e["ts"]) >= 19 else e["ts"]
        lines.append(f"[{short_ts}] {e['level']:<7} {e['logger']}: {e['message']}")
    return "\n".join(lines)
