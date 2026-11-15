import asyncio
import logging
import os
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("relentless.notify")

DEDUP_WINDOW_SECONDS = 600
_last_alerts = {}
_warned_missing_telegram = False


def _log_missing_telegram_once() -> None:
    global _warned_missing_telegram
    if not _warned_missing_telegram:
        _warned_missing_telegram = True
        logger.warning(
            "Telegram notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set."
        )


def _should_suppress(msg: str) -> bool:
    key = (msg or "")[:64]
    now = time.time()
    if key in _last_alerts and (now - _last_alerts[key]) < DEDUP_WINDOW_SECONDS:
        return True
    _last_alerts[key] = now
    return False


def _send_sync_post(base: str, chat_id: str, msg: str) -> None:
    try:
        resp = httpx.post(
            f"{base}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=10.0
        )
        if resp.status_code >= 400:
            logger.warning("Telegram alert HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Failed to send Telegram alert: %s", e)


def alert(msg: str) -> None:
    if _should_suppress(msg):
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        _log_missing_telegram_once()
        return
    base = f"https://api.telegram.org/bot{token}"

    try:
        asyncio.get_running_loop()
        t = threading.Thread(target=_send_sync_post, args=(base, chat_id, msg), daemon=True)
        t.start()
    except RuntimeError:
        _send_sync_post(base, chat_id, msg)


async def async_alert(msg: str) -> None:
    if _should_suppress(msg):
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        _log_missing_telegram_once()
        return
    base = f"https://api.telegram.org/bot{token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/sendMessage", json={"chat_id": chat_id, "text": msg}
            )
            if resp.status_code >= 400:
                logger.warning("Telegram async alert HTTP %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("Failed to send async Telegram alert: %s", e)
