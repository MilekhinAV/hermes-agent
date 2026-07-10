"""Regression tests for Telegram Bot API 10.0 Guest Mode.

Guest Mode delivers ``guest_message`` updates and requires replies via
``answerGuestQuery``. python-telegram-bot 22.6 does not expose first-class
attributes/methods for these yet, so Hermes must bridge raw update payloads.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
import plugins.platforms.telegram.adapter as telegram_platform
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.session import SessionSource
from gateway.platforms.base import _thread_metadata_for_source


def _make_adapter(*, guest_mode=True):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(
        enabled=True,
        token="***",
        extra={
            "guest_mode": guest_mode,
            "allowed_chats": ["-100-allowlisted-only"],
            "group_allowed_chats": [],
            "allowed_topics": [],
        },
    )
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot", _post=AsyncMock(return_value={"inline_message_id": "inline-1"}))
    adapter._message_handler = AsyncMock()
    adapter._pending_text_batches = {}
    adapter._pending_text_batch_tasks = {}
    adapter._text_batch_delay_seconds = 0.01
    adapter._text_batch_split_delay_seconds = 0.01
    adapter._mention_patterns = []
    adapter._forum_lock = None
    adapter._forum_command_registered = set()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._business_connections = {}
    adapter._is_callback_user_authorized = lambda user_id, **_kw: True
    return adapter


def _raw_guest_message(text="@hermes_bot скажи hello world на Python"):
    return {
        "message_id": 42,
        "date": 0,
        "chat": {"id": -100123, "type": "supergroup", "title": "Foreign chat"},
        "from": {"id": 111, "is_bot": False, "first_name": "Alice", "last_name": "Example"},
        "text": text,
        "guest_query_id": "guest-query-1",
    }


def _guest_message(raw_message=None):
    raw = raw_message or _raw_guest_message()
    user_raw = raw["from"]
    chat_raw = raw["chat"]
    return SimpleNamespace(
        message_id=raw["message_id"],
        date=None,
        chat=SimpleNamespace(
            id=chat_raw["id"],
            type=chat_raw["type"],
            title=chat_raw.get("title"),
            full_name=None,
            is_forum=False,
        ),
        from_user=SimpleNamespace(
            id=user_raw["id"],
            is_bot=user_raw.get("is_bot", False),
            first_name=user_raw.get("first_name"),
            full_name=" ".join(
                part for part in [user_raw.get("first_name"), user_raw.get("last_name")] if part
            ),
        ),
        text=raw.get("text"),
        caption=raw.get("caption"),
        entities=[],
        caption_entities=[],
        message_thread_id=None,
        is_topic_message=False,
        reply_to_message=None,
        quote=None,
        forum_topic_created=None,
        api_kwargs={"guest_query_id": raw["guest_query_id"]},
    )


def _guest_update(raw_message=None):
    return SimpleNamespace(
        update_id=777,
        message=None,
        effective_message=None,
        api_kwargs={"guest_message": raw_message or _raw_guest_message()},
    )


@pytest.mark.asyncio
async def test_guest_message_update_is_enqueued_from_raw_api_kwargs(monkeypatch):
    adapter = _make_adapter(guest_mode=True)
    adapter._enqueue_text_event = MagicMock()
    monkeypatch.setattr(
        telegram_platform.Message,
        "de_json",
        staticmethod(lambda raw, bot: _guest_message(raw)),
        raising=False,
    )

    await adapter._handle_guest_message(_guest_update(), SimpleNamespace())

    adapter._enqueue_text_event.assert_called_once()
    event = adapter._enqueue_text_event.call_args.args[0]
    assert event.message_type == MessageType.TEXT
    assert event.text == "скажи hello world на Python"
    assert event.platform_update_id == 777
    assert event.source.chat_type == "group"
    assert event.source.chat_id == "-100123"
    assert event.source.user_id == "111"
    assert event.source.telegram_guest_query_id == "guest-query-1"


def test_guest_query_id_is_preserved_when_building_event_from_message():
    adapter = _make_adapter(guest_mode=True)
    msg = _guest_message()

    event = adapter._build_message_event(msg, MessageType.TEXT, update_id=777)

    assert event.source.telegram_guest_query_id == "guest-query-1"


@pytest.mark.asyncio
async def test_send_uses_answer_guest_query_for_guest_metadata():
    adapter = _make_adapter(guest_mode=True)

    result = await adapter.send(
        "-100123",
        "Привет! Вот так:\n\nprint('Hello World')",
        metadata={"telegram_guest_query_id": "guest-query-1"},
    )

    assert result.success is True
    assert result.message_id == "inline-1"
    adapter._bot._post.assert_awaited_once()
    endpoint = adapter._bot._post.await_args.args[0]
    data = adapter._bot._post.await_args.kwargs["data"]
    assert endpoint == "answerGuestQuery"
    assert data["guest_query_id"] == "guest-query-1"
    payload = json.loads(data["result"])
    assert payload["type"] == "article"
    assert payload["input_message_content"]["message_text"].startswith("Привет!")


def test_thread_metadata_for_source_includes_guest_query_id():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        chat_type="group",
        user_id="111",
        telegram_guest_query_id="guest-query-1",
    )

    assert _thread_metadata_for_source(source) == {"telegram_guest_query_id": "guest-query-1"}


def test_allowed_updates_include_guest_message_until_ptb_supports_it():
    allowed = TelegramAdapter._allowed_updates_with_guest_messages()

    assert "guest_message" in {str(item) for item in allowed}
