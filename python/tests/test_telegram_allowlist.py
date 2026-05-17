"""Tests for the Telegram allowlist: handler-level allow/deny + warning on empty list."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

pytest.importorskip("telegram", reason="python-telegram-bot extra not installed")

from timberbot.user_api.telegram.handlers import make_handlers  # noqa: E402


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class _FakeQuery:
    def __init__(self, user_id: int, data: str) -> None:
        self.from_user = _FakeUser(user_id)
        self.data = data
        self.answered = False

    async def answer(self) -> None:
        self.answered = True


class _FakeUpdate:
    def __init__(
        self,
        user_id: int = 1,
        chat_id: int = 100,
        message: _FakeMessage | None = None,
        callback_query: _FakeQuery | None = None,
    ) -> None:
        self.effective_user = _FakeUser(user_id)
        self.effective_chat = _FakeChat(chat_id)
        self.message = message
        self.callback_query = callback_query


async def test_callback_handler_drops_disallowed_user() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    handlers = make_handlers(queue, allowed_users={42})
    query = _FakeQuery(user_id=999, data="choice:abc:Yes")
    update = _FakeUpdate(callback_query=query)

    await handlers["choice_callback"](update, MagicMock())

    assert queue.empty(), "disallowed user's callback should not enqueue UserMessage"
    assert query.answered is True, "should still answer the callback to dismiss the Telegram spinner"


async def test_callback_handler_allows_listed_user() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    handlers = make_handlers(queue, allowed_users={42})
    query = _FakeQuery(user_id=42, data="choice:abc:Yes")
    update = _FakeUpdate(user_id=42, callback_query=query)

    await handlers["choice_callback"](update, MagicMock())

    assert not queue.empty(), "allowed user should produce a UserMessage"
    msg = await queue.get()
    assert msg.text == "choice:abc:Yes"
    assert msg.user_id == "42"


async def test_callback_handler_no_allowlist_allows_anyone() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    handlers = make_handlers(queue, allowed_users=set())
    query = _FakeQuery(user_id=12345, data="choice:abc:Yes")
    update = _FakeUpdate(user_id=12345, callback_query=query)

    await handlers["choice_callback"](update, MagicMock())

    assert not queue.empty()


def test_telegram_adapter_warns_when_allowlist_empty(caplog: pytest.LogCaptureFixture) -> None:
    pytest.importorskip("telegram")
    from timberbot.user_api.telegram.bot import TelegramAdapter

    with caplog.at_level(logging.WARNING, logger="timberbot.user_api"):
        TelegramAdapter(token="1:fake", allowed_users=None)
    assert any("no allowed_users configured" in r.getMessage() for r in caplog.records)


def test_telegram_adapter_silent_with_allowlist(caplog: pytest.LogCaptureFixture) -> None:
    pytest.importorskip("telegram")
    from timberbot.user_api.telegram.bot import TelegramAdapter

    with caplog.at_level(logging.WARNING, logger="timberbot.user_api"):
        TelegramAdapter(token="1:fake", allowed_users=[123, 456])
    assert not any("no allowed_users configured" in r.getMessage() for r in caplog.records)
