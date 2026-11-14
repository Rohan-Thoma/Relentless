"""Zero-dependency single-user authentication and rate-limiting for Relentless Web API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from src.notify import alert

SESSION_COOKIE_NAME = "relentless_session"
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 900
LOCALHOST_IPS = {"127.0.0.1", "::1", "localhost", "testclient"}

_FAILED_ATTEMPTS: dict[str, list[float]] = {}


def get_configured_secret() -> str:
    """Returns configured API key or admin password from environment."""
    return (os.getenv("WEB_API_KEY") or os.getenv("WEB_ADMIN_PASSWORD") or "").strip()


def get_signing_key() -> bytes:
    """Derives a 32-byte HMAC signing key for stateless session tokens."""
    raw_secret = (
        os.getenv("WEB_SESSION_SECRET")
        or get_configured_secret()
        or "relentless-local-dev-key"
    )
    return hashlib.sha256(raw_secret.encode("utf-8")).digest()


def _prune_attempts(client_ip: str, now: float | None = None) -> list[float]:
    current = now if now is not None else time.time()
    cutoff = current - LOCKOUT_WINDOW_SECONDS
    recent = [ts for ts in _FAILED_ATTEMPTS.get(client_ip, []) if ts >= cutoff]
    if recent:
        _FAILED_ATTEMPTS[client_ip] = recent
    else:
        _FAILED_ATTEMPTS.pop(client_ip, None)
    return recent


def is_ip_locked_out(client_ip: str) -> bool:
    """Returns True if client_ip has reached the failed-attempt threshold inside the window."""
    return len(_prune_attempts(client_ip)) >= MAX_FAILED_ATTEMPTS


def record_failed_attempt(client_ip: str) -> int:
    """Records a failed login attempt and sends a Telegram alert on lockout threshold."""
    now = time.time()
    attempts = _prune_attempts(client_ip, now=now)
    attempts.append(now)
    _FAILED_ATTEMPTS[client_ip] = attempts
    count = len(attempts)
    if count == MAX_FAILED_ATTEMPTS:
        alert(f"🚨 Web API security lockout: 5 failed auth attempts from IP {client_ip}")
    return count


def clear_failed_attempts(client_ip: str) -> None:
    """Clears failed login attempts for client_ip after successful authentication."""
    _FAILED_ATTEMPTS.pop(client_ip, None)


def reset_rate_limiter() -> None:
    """Resets all tracked failed login attempts (intended for testing)."""
    _FAILED_ATTEMPTS.clear()


def create_session_token(ttl_seconds: int = 604800) -> str:
    """Creates an HMAC-SHA256 signed session token with explicit expiration."""
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"owner:{exp}:{secrets.token_hex(8)}"
    sig = hmac.new(get_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded}.{sig}"


def verify_session_token(token: str) -> bool:
    """Verifies an HMAC-SHA256 signed session token and checks its expiration."""
    if not isinstance(token, str) or "." not in token:
        return False
    encoded, sig = token.strip().split(".", 1)
    if not encoded or not sig:
        return False
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return False

    expected_sig = hmac.new(get_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False

    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "owner":
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    return exp > time.time()


def verify_secret(candidate: str) -> bool:
    """Constant-time comparison against configured WEB_API_KEY / WEB_ADMIN_PASSWORD."""
    configured = get_configured_secret()
    if not configured or not isinstance(candidate, str) or not candidate:
        return False
    return hmac.compare_digest(candidate, configured)


def _extract_cookie(cookie_header: str, name: str) -> str | None:
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        chunk = part.strip()
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return None


def authenticate_request(
    headers: dict[str, str], client_ip: str = "127.0.0.1"
) -> dict[str, Any]:
    """Evaluates API key, Bearer token, session cookie, or local dev mode."""
    norm = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    secret = get_configured_secret()

    if secret:
        api_key = norm.get("x-relentless-api-key", "").strip()
        if api_key and verify_secret(api_key):
            return {"authenticated": True, "mode": "api_key", "auth_required": True}

        auth_hdr = norm.get("authorization", "").strip()
        if auth_hdr.lower().startswith("bearer "):
            bearer = auth_hdr[7:].strip()
            if verify_secret(bearer) or verify_session_token(bearer):
                return {"authenticated": True, "mode": "bearer", "auth_required": True}

        cookie_val = _extract_cookie(norm.get("cookie", ""), SESSION_COOKIE_NAME)
        if cookie_val and verify_session_token(cookie_val):
            return {"authenticated": True, "mode": "session", "auth_required": True}

        return {"authenticated": False, "mode": "none", "auth_required": True}

    if client_ip in LOCALHOST_IPS and not norm.get("x-forwarded-for"):
        return {"authenticated": True, "mode": "local_dev", "auth_required": False}

    return {"authenticated": False, "mode": "unconfigured_remote", "auth_required": True}
