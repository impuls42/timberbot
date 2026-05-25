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


async def test_overflow_rolls_over_to_new_message(mock_bot: MagicMock) -> None:
    """Text beyond MAX_CHARS must spill into a new send_message, not retry edit."""
    new_msg = MagicMock()
    new_msg.message_id = 999
    mock_bot.send_message = AsyncMock(return_value=new_msg)

    buf = StreamBuffer(chat_id=123, bot=mock_bot, message_id=456)
    # Feed 1.5x the cap; expect: edit on #456 with the first MAX_CHARS,
    # then send a fresh message for the tail.
    huge = "abc " * (StreamBuffer.MAX_CHARS // 4 + 200)  # > MAX_CHARS
    buf.feed(huge)
    await buf.flush()

    mock_bot.edit_message_text.assert_called()
    edit_args = mock_bot.edit_message_text.call_args
    edit_text = edit_args.kwargs.get("text") or edit_args.args[0]
    assert len(edit_text) <= StreamBuffer.MAX_CHARS

    mock_bot.send_message.assert_called_once()
    send_text = mock_bot.send_message.call_args.kwargs.get("text", "")
    assert send_text  # tail must not be empty
    # Combined head + tail must equal what we fed (modulo split boundary).
    assert (edit_text + send_text).replace(" ", "") == huge.replace(" ", "")


async def test_overflow_prefers_whitespace_split(mock_bot: MagicMock) -> None:
    """Splits should land on a newline/space when one exists in lookback."""
    new_msg = MagicMock()
    new_msg.message_id = 999
    mock_bot.send_message = AsyncMock(return_value=new_msg)

    # Build text with a clear newline shortly before the cap.
    pad = "x" * (StreamBuffer.MAX_CHARS - 50)
    text = pad + "\nTAIL marker here that should land in a new message."
    buf = StreamBuffer(chat_id=123, bot=mock_bot, message_id=456)
    buf.feed(text)
    await buf.flush()

    edit_text = mock_bot.edit_message_text.call_args.kwargs.get("text", "")
    # The edit should end at or before the newline, not mid-line.
    assert not edit_text.startswith("TAIL")
    send_text = mock_bot.send_message.call_args.kwargs.get("text", "")
    assert "TAIL marker" in send_text


async def test_flush_skips_when_text_unchanged(buf: StreamBuffer, mock_bot: MagicMock) -> None:
    """Once the stream stops, further periodic flushes must not re-send the same text.

    Telegram rejects an edit whose content matches the current message with
    BadRequest('Message is not modified'); the periodic flush task would
    otherwise produce one error log per FLUSH_INTERVAL until the buffer is
    stopped.
    """
    buf.feed("hello")
    await buf.flush()
    assert mock_bot.edit_message_text.call_count == 1
    # Subsequent flushes with no new text should be no-ops.
    await buf.flush()
    await buf.flush()
    assert mock_bot.edit_message_text.call_count == 1
    # A new chunk does cause another edit.
    buf.feed(" world")
    await buf.flush()
    assert mock_bot.edit_message_text.call_count == 2
