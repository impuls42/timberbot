"""Tests for `_user_message_loop` — agent↔user wiring, eager session open,
control-command routing, soft-cancel vs hard-halt.
"""
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


class _FakeAdapter:
    """UserAdapter test double — drains a fixed input list, captures outgoing sends."""

    def __init__(self, inbound: list[UserMessage]) -> None:
        self._inbound = list(inbound)
        self.sent: list[Any] = []

    async def messages(self):
        for m in self._inbound:
            yield m

    async def send(self, msg: Any) -> None:
        self.sent.append(msg)


class _FakeSession:
    """Stand-in for connector.Session — exposes the surface the loop touches."""

    def __init__(self, session_id: str = "acp-sess-1") -> None:
        self.session_id = session_id
        self.state = "active"
        self.on_update = None
        self.on_elicitation = None
        self.on_tool_action = None
        self.prompts: list[str] = []
        self.cancelled = False

    async def prompt(self, text: str) -> None:
        self.prompts.append(text)

    async def cancel(self) -> None:
        self.cancelled = True


class _FakeConnection:
    """Stand-in for connector.AgentConnection — returns _FakeSession on new_session."""

    def __init__(self, session: _FakeSession | None = None) -> None:
        self.session = session or _FakeSession()
        self.closed = False

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[dict],
        allowed_tools: list[str] | None = None,
    ) -> _FakeSession:
        return self.session

    async def close(self) -> None:
        self.closed = True


class _FakeACP:
    """Stand-in for ACPConnector — connect() returns _FakeConnection."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.connection = _FakeConnection(self.session)

    async def connect(self, binary: str, model: str) -> _FakeConnection:
        return self.connection


@pytest.fixture
def cfg() -> ServeConfig:
    return ServeConfig(telegram_token="fake", telegram_dialog_id="42")


# --- eager open ---------------------------------------------------------


async def test_session_opens_eagerly_before_any_inbound_message(cfg: ServeConfig) -> None:
    """The loop opens the ACP session at startup so the bot can push
    preemptively. No inbound message is required to get to "active"."""
    adapter = _FakeAdapter([])  # no inbound messages
    acp = _FakeACP()

    await _user_message_loop(adapter, acp, cfg)

    actives = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "active"]
    assert len(actives) == 1, "should emit one 'active' state change at eager open"
    # No prompts were sent because no inbound user message arrived.
    assert acp.session.prompts == []


async def test_first_inbound_prompt_carries_bootstrap(cfg: ServeConfig) -> None:
    """The first user prompt of the eagerly-opened session is bootstrapped
    with the agent spec; subsequent prompts are plain user text."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="42", text="hi", chat_id=42),
        UserMessage(dialog_id="42", text="again", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, acp, cfg)

    first, second = acp.session.prompts
    assert "TIMBERBOT_SYSTEM_BOUNDARY" in first
    assert first.endswith("hi")
    assert "TIMBERBOT_SYSTEM_BOUNDARY" not in second
    assert second == "again"


async def test_callbacks_wired_at_eager_open(cfg: ServeConfig) -> None:
    """on_update / on_elicitation / on_tool_action are wired before any
    inbound message — so push paths can flow before the first user ping."""
    adapter = _FakeAdapter([])
    acp = _FakeACP()
    await _user_message_loop(adapter, acp, cfg)
    assert acp.session.on_update is not None
    assert acp.session.on_elicitation is not None
    assert acp.session.on_tool_action is not None


# --- /cancel (soft) vs /halt (hard) ------------------------------------


async def test_cancel_keeps_session_alive(cfg: ServeConfig) -> None:
    """Soft /cancel interrupts the in-flight turn but keeps the same
    AgentConnection alive — `acp.connect` is only ever awaited once."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="42", text="hi", chat_id=42),
        UserMessage(dialog_id="42", text="/cancel", chat_id=42),
        UserMessage(dialog_id="42", text="again", chat_id=42),
    ])
    acp = MagicMock()
    session = _FakeSession()
    conn = _FakeConnection(session)
    acp.connect = AsyncMock(return_value=conn)

    await _user_message_loop(adapter, acp, cfg)

    assert acp.connect.await_count == 1, "soft /cancel must not reconnect"
    assert session.cancelled is True
    assert session.prompts[0].endswith("hi")
    assert session.prompts[1] == "again"


async def test_halt_recycles_session_eagerly(cfg: ServeConfig) -> None:
    """Hard /halt tears down the AgentConnection AND immediately reopens
    so the bot stays reachable. Two `connect` awaits total."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="42", text="hi", chat_id=42),
        UserMessage(dialog_id="42", text="/halt", chat_id=42),
    ])
    s1 = _FakeSession("acp-sess-1")
    s2 = _FakeSession("acp-sess-2")
    c1 = _FakeConnection(s1)
    c2 = _FakeConnection(s2)
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[c1, c2])

    await _user_message_loop(adapter, acp, cfg)

    assert acp.connect.await_count == 2, "halt must recycle the connection"
    assert s1.cancelled is True
    # Two 'active' state changes — one at eager open, one after /halt
    # recycles. The user sees the bot recovering on its own.
    actives = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "active"]
    assert len(actives) == 2


# --- /status, /state ----------------------------------------------------


async def test_status_reports_active(cfg: ServeConfig) -> None:
    """The eager-open invariant means /status always sees an active session."""
    adapter = _FakeAdapter([UserMessage(dialog_id="42", text="/status", chat_id=42)])
    acp = _FakeACP()
    await _user_message_loop(adapter, acp, cfg)
    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    # active (eager open) → active (status reply)
    assert [s.state for s in states] == ["active", "active"]


async def test_state_command_uses_client_summary(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(dialog_id="42", text="/state", chat_id=42)])
    acp = _FakeACP()

    class _FakeSummary:
        settlement = "Folktails"
        faction = "Folktails"
        science = 42
        time = type("T", (), {"dayNumber": 7, "dayProgress": 0.5, "speed": 2})()
        weather = type("W", (), {
            "cycle": 1, "isHazardous": True,
            "hazardousWeatherDuration": 3, "temperateWeatherDuration": None,
        })()
        districts = []
        alerts = {"food": 1}
        buildings = {}

    client = MagicMock()
    client.summary = MagicMock(return_value=_FakeSummary())

    await _user_message_loop(adapter, acp, cfg, client)

    infos = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "info"]
    assert infos and "day 7" in (infos[0].detail or "")
    assert infos[0].dialog_id == "42"


# --- streaming callbacks ------------------------------------------------


async def test_on_update_callback_routes_chunk_to_adapter(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([])  # eager open is enough; no need for an inbound
    acp = _FakeACP()
    await _user_message_loop(adapter, acp, cfg)

    await acp.session.on_update("acp-sess-1", "Hello back!")  # type: ignore[misc]

    chunks = [m for m in adapter.sent if isinstance(m, TextChunk)]
    assert chunks == [TextChunk(session_id="acp-sess-1", text="Hello back!", dialog_id="42")]


async def test_on_elicitation_callback_routes_to_adapter(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([])
    acp = _FakeACP()
    await _user_message_loop(adapter, acp, cfg)

    await acp.session.on_elicitation("acp-sess-1", {  # type: ignore[misc]
        "question": "Build farm here?",
        "choices": ["Yes", "No"],
        "correlationId": "elic-1",
    })

    elicits = [m for m in adapter.sent if isinstance(m, GameElicitation)]
    assert len(elicits) == 1
    e = elicits[0]
    assert e.question == "Build farm here?"
    assert e.dialog_id == "42"


# --- elicitation choice rewriting --------------------------------------


async def test_elicitation_choice_rewritten_as_prompt(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="42", text="choice:elic-1:Yes", chat_id=42),
    ])
    acp = _FakeACP()
    await _user_message_loop(adapter, acp, cfg)

    last_prompt = acp.session.prompts[-1]
    assert "Yes" in last_prompt
    assert "elic-1" in last_prompt


# --- broker wiring ------------------------------------------------------


class _RecordingBroker:
    """Test double for `SubagentBroker` — only the surface the loop touches."""

    def __init__(self) -> None:
        self.bound = 0
        self.unbound = 0

    def bind(self, conn, agent_cwd, mcp_servers, **_):
        self.bound += 1

    async def unbind(self):
        self.unbound += 1

    def state(self):
        return None


async def test_broker_bound_eagerly_at_startup(cfg: ServeConfig) -> None:
    """Broker.bind() runs once at eager open — before any inbound
    message — so delegate-family MCP tools work before the user pings."""
    adapter = _FakeAdapter([])
    acp = _FakeACP()
    broker = _RecordingBroker()

    await _user_message_loop(adapter, acp, cfg, broker=broker)

    assert broker.bound == 1
    assert broker.unbound == 1, "unbind runs on loop teardown"


async def test_broker_rebound_after_halt(cfg: ServeConfig) -> None:
    """/halt recycles the connection, which means unbind + rebind."""
    adapter = _FakeAdapter([UserMessage(dialog_id="42", text="/halt", chat_id=42)])
    s1, s2 = _FakeSession("acp-sess-1"), _FakeSession("acp-sess-2")
    c1, c2 = _FakeConnection(s1), _FakeConnection(s2)
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[c1, c2])
    broker = _RecordingBroker()

    await _user_message_loop(adapter, acp, cfg, broker=broker)

    assert broker.bound == 2
    assert broker.unbound == 2


async def test_broker_kept_bound_across_soft_cancel(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="42", text="hi", chat_id=42),
        UserMessage(dialog_id="42", text="/cancel", chat_id=42),
        UserMessage(dialog_id="42", text="again", chat_id=42),
    ])
    acp = _FakeACP()
    broker = _RecordingBroker()

    await _user_message_loop(adapter, acp, cfg, broker=broker)

    assert broker.bound == 1, "soft /cancel keeps the same binding"
    assert broker.unbound == 1


# --- prompt error path --------------------------------------------------


async def test_prompt_error_emits_error_state_to_user(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(dialog_id="42", text="hi", chat_id=42)])
    session = _FakeSession()
    session.prompt = AsyncMock(side_effect=RuntimeError("agent crashed"))  # type: ignore[method-assign]
    conn = _FakeConnection(session)
    acp = MagicMock()
    acp.connect = AsyncMock(return_value=conn)

    await _user_message_loop(adapter, acp, cfg)

    errors = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "error"]
    assert errors
    assert "agent crashed" in (errors[0].detail or "")


# --- reset_stream -------------------------------------------------------


async def test_reset_stream_called_before_each_prompt(cfg: ServeConfig) -> None:
    class _AdapterWithReset(_FakeAdapter):
        def __init__(self, inbound):
            super().__init__(inbound)
            self.resets: list[str] = []

        def reset_stream(self, session_id: str) -> None:
            self.resets.append(session_id)

    adapter = _AdapterWithReset([
        UserMessage(dialog_id="42", text="first", chat_id=42),
        UserMessage(dialog_id="42", text="second", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, acp, cfg)

    assert adapter.resets == ["acp-sess-1", "acp-sess-1"]
