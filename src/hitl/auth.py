"""Telegram sender authorization helpers for the single-user Relentless bot."""

import logging
import os

logger = logging.getLogger(__name__)


def configured_chat_id():
    """Return the configured Telegram chat id as int, or None if unset/invalid."""
    raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def update_chat_id(update):
    """Extract the originating chat id from a Telegram update, or None."""
    if not isinstance(update, dict):
        return None
    msg = update.get("message")
    if isinstance(msg, dict):
        chat = msg.get("chat")
        if isinstance(chat, dict) and "id" in chat:
            try:
                return int(chat["id"])
            except (TypeError, ValueError):
                return None
    cb = update.get("callback_query")
    if isinstance(cb, dict):
        cb_msg = cb.get("message")
        if isinstance(cb_msg, dict):
            chat = cb_msg.get("chat")
            if isinstance(chat, dict) and "id" in chat:
                try:
                    return int(chat["id"])
                except (TypeError, ValueError):
                    return None
    return None


def is_authorized(update, configured=None):
    """Return True only when the update originates from the configured chat."""
    if configured is None:
        configured = configured_chat_id()
    if configured is None:
        logger.warning("Telegram update rejected: TELEGRAM_CHAT_ID not configured")
        return False
    chat_id = update_chat_id(update)
    if chat_id is None:
        logger.warning("Telegram update rejected: missing chat id")
        return False
    if chat_id != configured:
        logger.warning("Telegram update rejected: unauthorized chat")
        return False
    return True
