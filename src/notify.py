import asyncio
import os
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

DEDUP_WINDOW_SECONDS = 600
_last_alerts = {}


def _should_suppress(msg: str) -> bool:
    key = (msg or "")[:64]
    now = time.time()
    if key in _last_alerts and (now - _last_alerts[key]) < DEDUP_WINDOW_SECONDS:
        return True
    _last_alerts[key] = now
    return False


def _send_sync_post(base: str, chat_id: str, msg: str) -> None:
    try:
        httpx.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=10.0)
    except Exception:
        pass


def alert(msg: str) -> None:
    if _should_suppress(msg):
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
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
        return
    base = f"https://api.telegram.org/bot{token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": msg})
    except Exception:
        pass
