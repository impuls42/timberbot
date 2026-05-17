"""Tests for `_user_message_loop` — agent→user wiring and control-command routing."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from timberbot.user_api.protocol import (
    GameElicitation,
    SessionStateChange,
    TextChunk,
    UserMessage,
)
from timberbot.user_api.serve import ServeConfig, _user_message_loop
from timberbot.user_api.session_manager import SessionManager


class _FakeAdapter:
    """UserAdapter test double — drains a fixed input list, captures outgoing sends."""

    def __init__(self, inbound: list[UserMessage]) -> None:
        self._inbound = list(inbound)
        self.sent: list[Any] = []
        self.registered: list[tuple[str, int]] = []

    async def messages(self):
        for m in self._inbound:
            yield m

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)

    def register_chat(self, session_id: str, chat_id: int) -> None:
        self.registered.append((session_id, chat_id))


class _FakeHandle:
    state = "active"

    def __init__(self) -> None:
        self.on_update = None
        self.on_elicitation = None
        self.prompts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def new_session(self, cwd: str, mcp_servers: list[dict]) -> str:
        return "acp-sess-1"

    async def prompt(self, session_id: str, text: str) -> None:
        self.prompts.append((session_id, text))

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class _FakeACP:
    def __init__(self) -> None:
        self.handle = _FakeHandle()

    async def connect(self, binary: str, model: str) -> _FakeHandle:
        return self.handle


@pytest.fixture
def cfg() -> ServeConfig:
    return ServeConfig(telegram_token="fake")


async def test_first_prompt_connects_wires_callbacks_and_dispatches(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(user_id="u1", text="hello agent", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.handle.prompts == [("acp-sess-1", "hello agent")]
    assert acp.handle.on_update is not None, "on_update callback must be wired"
    assert acp.handle.on_elicitation is not None, "on_elicitation callback must be wired"
    assert ("acp-sess-1", 42) in adapter.registered, "ACP session_id must be chat-registered"
    assert any(isinstance(m, SessionStateChange) and m.state == "active" for m in adapter.sent)


async def test_on_update_callback_routes_chunk_to_adapter(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(user_id="u1", text="hi", chat_id=42)])
    acp = _FakeACP()
    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # Simulate the agent emitting a streaming chunk via session/update.
    await acp.handle.on_update("acp-sess-1", "Hello back!")  # type: ignore[misc]

    chunks = [m for m in adapter.sent if isinstance(m, TextChunk)]
    assert chunks == [TextChunk(session_id="acp-sess-1", text="Hello back!")]


async def test_on_elicitation_callback_routes_to_adapter(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(user_id="u1", text="start", chat_id=42)])
    acp = _FakeACP()
    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    await acp.handle.on_elicitation("acp-sess-1", {  # type: ignore[misc]
        "question": "Build farm here?",
        "choices": ["Yes", "No"],
        "correlationId": "elic-1",
    })

    elicits = [m for m in adapter.sent if isinstance(m, GameElicitation)]
    assert len(elicits) == 1
    e = elicits[0]
    assert e.question == "Build farm here?"
    assert e.choices == ["Yes", "No"]
    assert e.correlation_id == "elic-1"


async def test_cancel_command_invokes_handle_cancel_not_prompt(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="hi", chat_id=42),
        UserMessage(user_id="u1", text="/cancel", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.handle.cancelled == ["acp-sess-1"]
    # The /cancel text should NOT have been forwarded as a prompt
    assert acp.handle.prompts == [("acp-sess-1", "hi")]
    # User should see a halting state change
    halting = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "halting"]
    assert halting, "user should be told the cancel was acked"


async def test_halt_command_also_cancels(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="hi", chat_id=42),
        UserMessage(user_id="u1", text="/halt", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.handle.cancelled == ["acp-sess-1"]


async def test_status_when_no_session(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(user_id="u1", text="/status", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # No /status should ever trigger ACP connect — connect() not called
    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    assert states and states[0].state == "no session"
    # No prompts sent to the agent either
    assert acp.handle.prompts == []


async def test_status_when_session_active(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="hi", chat_id=42),
        UserMessage(user_id="u1", text="/status", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    # First: "active" on connect. Second: "active" on /status.
    assert len(states) == 2
    assert states[1].state == "active"


async def test_elicitation_choice_rewritten_as_prompt(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="hi", chat_id=42),
        UserMessage(user_id="u1", text="choice:elic-1:Yes", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # The choice should be forwarded as a prompt the agent can read.
    last_prompt = acp.handle.prompts[-1]
    assert last_prompt[0] == "acp-sess-1"
    assert "Yes" in last_prompt[1]
    assert "elic-1" in last_prompt[1]


async def test_second_message_reuses_handle(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="first", chat_id=42),
        UserMessage(user_id="u1", text="second", chat_id=42),
    ])
    acp = MagicMock()
    handle = _FakeHandle()
    acp.connect = AsyncMock(return_value=handle)

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    acp.connect.assert_awaited_once()
    assert handle.prompts == [("acp-sess-1", "first"), ("acp-sess-1", "second")]


async def test_prompt_after_cancel_reconnects(cfg: ServeConfig) -> None:
    """After /cancel, the next /prompt must reconnect, not reuse the cancelled handle."""
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="first", chat_id=42),
        UserMessage(user_id="u1", text="/cancel", chat_id=42),
        UserMessage(user_id="u1", text="second", chat_id=42),
    ])
    handle1 = _FakeHandle()
    handle2 = _FakeHandle()
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[handle1, handle2])

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.connect.await_count == 2, "should reconnect after /cancel"
    assert handle1.prompts == [("acp-sess-1", "first")]
    assert handle1.cancelled == ["acp-sess-1"]
    assert handle2.prompts == [("acp-sess-1", "second")]


async def test_status_after_cancel_says_no_session(cfg: ServeConfig) -> None:
    """After /cancel the handle is evicted, so /status sees no session."""
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="first", chat_id=42),
        UserMessage(user_id="u1", text="/cancel", chat_id=42),
        UserMessage(user_id="u1", text="/status", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    # active (connect) → halting (cancel ack) → no session (status)
    assert states[-1].state == "no session"


async def test_stale_ended_handle_is_evicted(cfg: ServeConfig) -> None:
    """If the handle's state is ENDED (e.g. agent process died), the next prompt reconnects."""
    adapter = _FakeAdapter([
        UserMessage(user_id="u1", text="first", chat_id=42),
        UserMessage(user_id="u1", text="second", chat_id=42),
    ])
    handle1 = _FakeHandle()
    handle1.state = "ended"  # simulate agent dying after the first prompt completed
    handle2 = _FakeHandle()
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[handle1, handle2])

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.connect.await_count == 2, "ended handle must be evicted on next prompt"
    assert handle2.prompts == [("acp-sess-1", "second")]


async def test_prompt_error_emits_error_state_to_user(cfg: ServeConfig) -> None:
    """If handle.prompt() raises, the user gets a SessionStateChange(state='error')."""
    adapter = _FakeAdapter([UserMessage(user_id="u1", text="hi", chat_id=42)])
    handle = _FakeHandle()
    handle.prompt = AsyncMock(side_effect=RuntimeError("agent crashed"))  # type: ignore[method-assign]
    acp = MagicMock()
    acp.connect = AsyncMock(return_value=handle)

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    errors = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "error"]
    assert errors, "user must be told when the agent fails"
    assert "agent crashed" in (errors[0].detail or "")
