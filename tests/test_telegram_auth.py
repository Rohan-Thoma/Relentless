import os

from src.hitl.auth import is_authorized, update_chat_id


def test_authorized_message():
    os.environ["TELEGRAM_CHAT_ID"] = "123"
    upd = {"message": {"chat": {"id": 123}, "text": "hi"}}
    assert is_authorized(upd) is True


def test_unauthorized_message():
    os.environ["TELEGRAM_CHAT_ID"] = "123"
    upd = {"message": {"chat": {"id": 999}, "text": "hi"}}
    assert is_authorized(upd) is False


def test_missing_chat_config_denies():
    os.environ["TELEGRAM_CHAT_ID"] = ""
    upd = {"message": {"chat": {"id": 123}, "text": "hi"}}
    assert is_authorized(upd) is False


def test_callback_uses_message_chat():
    os.environ["TELEGRAM_CHAT_ID"] = "123"
    upd = {"callback_query": {"id": "1", "message": {"chat": {"id": 123}}}}
    assert is_authorized(upd) is True
    assert update_chat_id(upd) == 123


def test_callback_missing_chat_denies():
    os.environ["TELEGRAM_CHAT_ID"] = "123"
    upd = {"callback_query": {"id": "1"}}
    assert is_authorized(upd) is False