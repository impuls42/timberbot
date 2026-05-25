"""Tests for the Phase 2 subagent message routing in `TelegramAdapter`.

Covers:
- `ToolAction.subagent_id` → `[<id>] <summary>` prefix in chat
- `SubagentStatusChange` → one concise line per terminal transition
- `SubagentStatusChange` for non-terminal states (running, idle) is filtered out
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from timberbot.user_api.protocol import SubagentStatusChange, ToolAction
from timberbot.user_api.telegram.bot import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    # Constructing a real `Application` requires a token + network, so stub
    # the builder out and assemble the adapter with mocks.
    with patch("timberbot.user_api.telegram.bot.Application") as app_cls:
        app = MagicMock()
        app.bot = MagicMock()
        app.bot.send_message = AsyncMock()
        app_cls.builder.return_value.token.return_value.build.return_value = app
        return TelegramAdapter(token="fake", allowed_users=[42])


@pytest.mark.asyncio
async def test_tool_action_prefixes_with_subagent_id():
    adapter = _make_adapter()
    adapter._chat_ids["acp-main"] = 1001
    msg = ToolAction(
        session_id="acp-main",
        summary="✅ place_building(prefab=LogPile, x=10)",
        ok=True,
        user_id="u1",
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
        user_id="u1",
    )
    await adapter.send(msg)
    kwargs = adapter._app.bot.send_message.await_args.kwargs
    assert not kwargs["text"].startswith("[")


@pytest.mark.asyncio
async def test_subagent_status_terminal_emits_one_line():
    adapter = _make_adapter()
    adapter._chat_by_user["u1"] = 1001
    msg = SubagentStatusChange(
        user_id="u1",
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
    adapter._chat_by_user["u1"] = 1001
    msg = SubagentStatusChange(
        user_id="u1",
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
    adapter._chat_by_user["u1"] = 1001
    msg = SubagentStatusChange(
        user_id="u1",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="idle",
        new_status="running",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_subagent_status_drops_when_no_chat_bound():
    """Without a chat_by_user entry the message is logged-and-dropped, not crash-y."""
    adapter = _make_adapter()
    msg = SubagentStatusChange(
        user_id="u1",
        subagent_id="scout-a8f3",
        agent="scout",
        prev_status="running",
        new_status="completed",
    )
    await adapter.send(msg)
    adapter._app.bot.send_message.assert_not_awaited()
