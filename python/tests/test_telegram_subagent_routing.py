"""Tests for the subagent message routing in `TelegramAdapter`.

Covers:
- `ToolAction.subagent_id` → `[<id>] <summary>` prefix in chat
- `SubagentStatusChange` → one concise line per terminal transition
- Subagent `TextChunk` streams open their own buffer keyed `session_id#subagent_id`
- `probe()` raises `DialogUnreachableError` on bad chat ids

The adapter is bound to a single chat id (42) at construction; every
outbound message routes there without any per-message dialog_id lookup.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

from timberbot.user_api.protocol import SubagentStatusChange, TextChunk, ToolAction
from timberbot.user_api.telegram.bot import DialogUnreachableError, TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    # Constructing a real `Application` requires a token + network, so stub
    # the builder out and assemble the adapter with mocks.
    with patch("timberbot.user_api.telegram.bot.Application") as app_cls:
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        app.bot.get_chat = AsyncMock()
        app_cls.builder.return_value.token.return_value.build.return_value = app
        return TelegramAdapter(token="fake", dialog_id="42")


@pytest.mark.asyncio
async def test_tool_action_prefixes_with_subagent_id():
    adapter = _make_adapter()
    msg = ToolAction(
        session_id="acp-main",
        summary="✅ place_building(prefab=LogPile, x=10)",
        ok=True,
        dialog_id="42",
        subagent_id="scout-a8f3",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_awaited_once()
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["text"].startswith("[scout-a8f3] ")
    assert "place_building" in kwargs["text"]


@pytest.mark.asyncio
async def test_tool_action_no_prefix_when_not_subagent():
    adapter = _make_adapter()
    msg = ToolAction(
        session_id="acp-main",
        summary="✅ place_building(prefab=LogPile)",
        ok=True,
        dialog_id="42",
    )
    await adapter.send(msg)
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert not kwargs["text"].startswith("[")


@pytest.mark.asyncio
async def test_subagent_status_terminal_emits_one_line():
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="42",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="running",
        new_status="completed",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_awaited_once()
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert "scout-a8f3" in kwargs["text"]
    assert "completed" in kwargs["text"]


@pytest.mark.asyncio
async def test_subagent_status_errored_includes_detail():
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="42",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="running",
        new_status="errored",
        detail="timeout after 60s",
    )
    await adapter.send(msg)
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert "errored" in kwargs["text"]
    assert "timeout after 60s" in kwargs["text"]


@pytest.mark.asyncio
async def test_subagent_status_running_is_filtered_out():
    """`idle → running` is too noisy to surface; the adapter drops it."""
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="42",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="idle",
        new_status="running",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_subagent_text_chunk_opens_prefixed_stream():
    """A subagent's `TextChunk` opens its own stream buffer under a
    `[<subagent_id>] …` placeholder, distinct from the main-session
    buffer. Lets the user watch subagent reasoning live without
    overwriting the main agent's reply."""
    adapter = _make_adapter()
    msg = TextChunk(
        session_id="sub-sess",
        text="hello from scout",
        dialog_id="42",
        subagent_id="scout-a8f3",
    )
    await adapter.send(msg)
    sent = adapter._app.bot.send_message.await_args
    assert sent.kwargs["text"] == "[scout-a8f3] …"
    assert "sub-sess#scout-a8f3" in adapter._buffers


# --- probe() startup check ---------------------------------------------


@pytest.mark.asyncio
async def test_probe_succeeds_when_chat_visible():
    adapter = _make_adapter()
    await adapter.probe()
    adapter._app.bot.get_chat.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_probe_raises_on_bad_request():
    adapter = _make_adapter()
    adapter._app.bot.get_chat = AsyncMock(side_effect=BadRequest("chat not found"))
    with pytest.raises(DialogUnreachableError) as excinfo:
        await adapter.probe()
    assert "42" in str(excinfo.value)
    assert "chat not found" in str(excinfo.value)


# --- constructor validation --------------------------------------------


def test_adapter_rejects_empty_dialog_id():
    with (
        patch("timberbot.user_api.telegram.bot.Application"),
        pytest.raises(ValueError, match="non-empty dialog_id"),
    ):
        TelegramAdapter(token="fake", dialog_id="")


def test_adapter_rejects_non_numeric_dialog_id():
    with (
        patch("timberbot.user_api.telegram.bot.Application"),
        pytest.raises(ValueError, match="numeric Telegram chat id"),
    ):
        TelegramAdapter(token="fake", dialog_id="not-a-number")
