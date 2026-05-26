"""Tests for the Phase 2 subagent message routing in `TelegramAdapter`.

Covers:
- `ToolAction.subagent_id` → `[<id>] <summary>` prefix in chat
- `SubagentStatusChange` → one concise line per terminal transition
- `SubagentStatusChange` for non-terminal states (running, idle) is filtered out
- Subagent `TextChunk` streams open their own buffer with a `[<id>] …` placeholder
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timberbot.user_api.protocol import SubagentStatusChange, TextChunk, ToolAction
from timberbot.user_api.telegram.bot import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    # Constructing a real `Application` requires a token + network, so stub
    # the builder out and assemble the adapter with mocks.
    with patch("timberbot.user_api.telegram.bot.Application") as app_cls:
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
        app_cls.builder.return_value.token.return_value.build.return_value = app
        return TelegramAdapter(token="fake", allowed_dialogs=[42])


@pytest.mark.asyncio
async def test_tool_action_prefixes_with_subagent_id():
    adapter = _make_adapter()
    adapter._chat_ids["acp-main"] = 1001
    msg = ToolAction(
        session_id="acp-main",
        summary="✅ place_building(prefab=LogPile, x=10)",
        ok=True,
        dialog_id="1001",
        subagent_id="scout-a8f3",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_awaited_once()
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert kwargs["text"].startswith("[scout-a8f3] ")
    assert "place_building" in kwargs["text"]


@pytest.mark.asyncio
async def test_tool_action_no_prefix_when_not_subagent():
    adapter = _make_adapter()
    adapter._chat_ids["acp-main"] = 1001
    msg = ToolAction(
        session_id="acp-main",
        summary="✅ place_building(prefab=LogPile)",
        ok=True,
        dialog_id="1001",
    )
    await adapter.send(msg)
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert not kwargs["text"].startswith("[")


@pytest.mark.asyncio
async def test_subagent_status_terminal_emits_one_line():
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="1001",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="running",
        new_status="completed",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_awaited_once()
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 1001
    assert "scout-a8f3" in kwargs["text"]
    assert "completed" in kwargs["text"]


@pytest.mark.asyncio
async def test_subagent_status_errored_includes_detail():
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="1001",
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
    """`idle → running` is too noisy to surface; the adapter must drop it."""
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="1001",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="idle",
        new_status="running",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_subagent_status_drops_when_dialog_id_invalid():
    """Garbage `dialog_id` is logged-and-dropped, not crash-y."""
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        dialog_id="not-a-number",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="running",
        new_status="completed",
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
    # Main-session chat binding so the first chunk can resolve a chat.
    adapter._chat_ids["acp-main"] = 1001
    msg = TextChunk(
        session_id="sub-sess",
        text="hello from scout",
        dialog_id="1001",
        subagent_id="scout-a8f3",
    )
    await adapter.send(msg)
    # The placeholder send was awaited with the subagent prefix.
    sent_args = adapter._app.bot.send_message.await_args
    assert sent_args.kwargs["text"] == "[scout-a8f3] …"
    # A buffer was registered under the subagent's keyed slot.
    assert "sub-sess#scout-a8f3" in adapter._buffers
