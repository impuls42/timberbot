"""Tests for StreamBuffer — the throttled Telegram message-edit helper."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from timberbot.user_api.telegram.streaming import StreamBuffer


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    return bot


@pytest.fixture
def buf(mock_bot: MagicMock) -> StreamBuffer:
    return StreamBuffer(chat_id=123, bot=mock_bot, message_id=456)


async def test_feed_below_threshold_no_flush(buf: StreamBuffer, mock_bot: MagicMock) -> None:
    buf.feed("x" * 100)
    await asyncio.sleep(0)
    mock_bot.edit_message_text.assert_not_called()


async def test_feed_over_threshold_flushes(buf: StreamBuffer, mock_bot: MagicMock) -> None:
    for _ in range(6):
        buf.feed("x" * 100)
    await asyncio.sleep(0)
    mock_bot.edit_message_text.assert_called()


async def test_stop_does_final_flush(buf: StreamBuffer, mock_bot: MagicMock) -> None:
    buf.feed("hello world")
    await buf.stop()
    mock_bot.edit_message_text.assert_called_once()
    call_kwargs = mock_bot.edit_message_text.call_args
    assert "hello world" in call_kwargs.kwargs.get("text", "") or "hello world" in str(call_kwargs)


async def test_periodic_flush(mock_bot: MagicMock) -> None:
    buf = StreamBuffer(chat_id=123, bot=mock_bot, message_id=456)
    await buf.start()
    buf.feed("small chunk")
    await asyncio.sleep(0.7)
    await buf.stop()
    mock_bot.edit_message_text.assert_called()


async def test_flush_with_no_message_id_is_noop(mock_bot: MagicMock) -> None:
    buf = StreamBuffer(chat_id=123, bot=mock_bot, message_id=None)
    buf.feed("x" * 600)
    await asyncio.sleep(0)
    await buf.flush()
    mock_bot.edit_message_text.assert_not_called()
