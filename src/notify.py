import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

DEDUP_WINDOW_SECONDS = 600
_last_alerts = {}


def alert(msg: str):
    key = (msg or "")[:64]
    now = time.time()
    if key in _last_alerts and (now - _last_alerts[key]) < DEDUP_WINDOW_SECONDS:
        return
    _last_alerts[key] = now

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    base = f"https://api.telegram.org/bot{token}"
    try:
        httpx.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": msg}, timeout=10.0)
    except Exception:
        pass