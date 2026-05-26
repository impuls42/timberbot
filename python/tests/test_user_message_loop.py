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

    # Convenience accessor for tests that grew up with the old single-handle API.
    @property
    def prompts(self) -> list[tuple[str, str]]:
        return [(self.session.session_id, p) for p in self.session.prompts]


@pytest.fixture
def cfg() -> ServeConfig:
    return ServeConfig(telegram_token="fake")


async def test_first_prompt_connects_wires_callbacks_and_dispatches(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="hello agent", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # First turn of a new session is prefixed with the agent-spec bootstrap;
    # the user's actual text is the trailing line.
    assert len(acp.session.prompts) == 1
    prompt = acp.session.prompts[0]
    assert prompt.endswith("hello agent")
    assert acp.session.on_update is not None, "on_update callback must be wired"
    assert acp.session.on_elicitation is not None, "on_elicitation callback must be wired"
    assert ("acp-sess-1", 42) in adapter.registered, "ACP session_id must be chat-registered"
    assert any(isinstance(m, SessionStateChange) and m.state == "active" for m in adapter.sent)


async def test_on_update_callback_routes_chunk_to_adapter(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="hi", chat_id=42)])
    acp = _FakeACP()
    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # Simulate the agent emitting a streaming chunk via session/update.
    await acp.session.on_update("acp-sess-1", "Hello back!")  # type: ignore[misc]

    chunks = [m for m in adapter.sent if isinstance(m, TextChunk)]
    assert chunks == [TextChunk(session_id="acp-sess-1", text="Hello back!", dialog_id="u1")]


async def test_on_elicitation_callback_routes_to_adapter(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="start", chat_id=42)])
    acp = _FakeACP()
    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    await acp.session.on_elicitation("acp-sess-1", {  # type: ignore[misc]
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


async def test_cancel_command_invokes_session_cancel_not_prompt(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="hi", chat_id=42),
        UserMessage(dialog_id="u1", text="/cancel", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.session.cancelled is True
    # The /cancel text should NOT have been forwarded as a prompt.
    # The first (and only) user prompt is the spec-bootstrapped "hi" turn.
    assert len(acp.session.prompts) == 1
    assert acp.session.prompts[0].endswith("hi")
    # User should see a halting state change
    halting = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "halting"]
    assert halting, "user should be told the cancel was acked"


async def test_halt_command_also_cancels(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="hi", chat_id=42),
        UserMessage(dialog_id="u1", text="/halt", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert acp.session.cancelled is True


async def test_status_when_no_session(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="/status", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # No /status should ever trigger ACP connect — connect() not called
    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    assert states and states[0].state == "no session"
    # No prompts sent to the agent either
    assert acp.session.prompts == []


async def test_status_when_session_active(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="hi", chat_id=42),
        UserMessage(dialog_id="u1", text="/status", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    # First: "active" on connect. Second: "active" on /status.
    assert len(states) == 2
    assert states[1].state == "active"


async def test_elicitation_choice_rewritten_as_prompt(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="hi", chat_id=42),
        UserMessage(dialog_id="u1", text="choice:elic-1:Yes", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # The choice should be forwarded as a prompt the agent can read.
    last_prompt = acp.session.prompts[-1]
    assert "Yes" in last_prompt
    assert "elic-1" in last_prompt


async def test_second_message_reuses_session(cfg: ServeConfig) -> None:
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    acp = _FakeACP()
    # Track that connect is only awaited once.
    acp.connect = AsyncMock(return_value=acp.connection)  # type: ignore[method-assign]

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    acp.connect.assert_awaited_once()
    # First prompt: bootstrap-prefixed; second: plain user text (bootstrap
    # is only injected on session creation).
    assert len(acp.session.prompts) == 2
    assert acp.session.prompts[0].endswith("first")
    assert acp.session.prompts[1] == "second"


async def test_prompt_after_soft_cancel_reuses_session(cfg: ServeConfig) -> None:
    """Phase 2: `/cancel` is the soft semantic — it interrupts the in-flight
    turn but keeps the AgentConnection + Session alive so the next user
    message reuses them (preserving conversation context and any subagents).
    The hard tear-down lives on `/halt`."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="/cancel", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    session1 = _FakeSession("acp-sess-1")
    session2 = _FakeSession("acp-sess-2")
    conn1 = _FakeConnection(session1)
    conn2 = _FakeConnection(session2)
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[conn1, conn2])

    await _user_message_loop(adapter, SessionManager(), acp, ServeConfig(telegram_token="fake"))

    assert acp.connect.await_count == 1, "soft /cancel must not reconnect"
    # The same session handled both prompts; only the first carries the bootstrap.
    assert session1.prompts[0].endswith("first")
    assert session1.cancelled is True
    assert session1.prompts[1] == "second"


async def test_prompt_after_halt_reconnects(cfg: ServeConfig) -> None:
    """Phase 2: `/halt` is the hard tear-down — it cancels AND evicts the
    AgentConnection. The next user message reconnects from scratch with a
    fresh bootstrap-prefixed first prompt."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="/halt", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    session1 = _FakeSession("acp-sess-1")
    session2 = _FakeSession("acp-sess-2")
    conn1 = _FakeConnection(session1)
    conn2 = _FakeConnection(session2)
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[conn1, conn2])

    await _user_message_loop(adapter, SessionManager(), acp, ServeConfig(telegram_token="fake"))

    assert acp.connect.await_count == 2, "should reconnect after /halt"
    assert session1.cancelled is True
    assert session2.prompts[0].endswith("second")


async def test_status_after_cancel_remains_active(cfg: ServeConfig) -> None:
    """After soft /cancel the session is reused, so /status still reports the
    same session as active. (Pre-Phase 2 this said 'no session' because
    cancel evicted; the soft semantic flipped that.)"""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="/cancel", chat_id=42),
        UserMessage(dialog_id="u1", text="/status", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    # active (connect) → halting (cancel ack) → active (status; soft cancel
    # restored the state).
    assert states[-1].state == "active"


async def test_stale_ended_session_is_evicted(cfg: ServeConfig) -> None:
    """If the session's state is ENDED (e.g. agent process died), the next prompt reconnects."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    session1 = _FakeSession("acp-sess-1")
    session1.state = "ended"  # simulate agent dying after the first prompt completed
    session2 = _FakeSession("acp-sess-2")
    conn1 = _FakeConnection(session1)
    conn2 = _FakeConnection(session2)
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[conn1, conn2])

    await _user_message_loop(adapter, SessionManager(), acp, ServeConfig(telegram_token="fake"))

    assert acp.connect.await_count == 2, "ended session must be evicted on next prompt"
    # Fresh session → bootstrap-prefixed prompt for the second turn.
    assert session2.prompts[0].endswith("second")


async def test_cancel_without_session_replies_no_session(cfg: ServeConfig) -> None:
    """/cancel before any prompt now sends a 'no session' reply instead of going silent."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="/cancel", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    states = [m for m in adapter.sent if isinstance(m, SessionStateChange)]
    assert states, "user must see a reply even when there's nothing to cancel"
    assert states[0].state == "no session"
    assert states[0].dialog_id == "u1"


async def test_state_command_uses_client_summary(cfg: ServeConfig) -> None:
    """`/state` queries TimberbotClient.summary() and sends the formatted dashboard."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="/state", chat_id=42)])
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

    await _user_message_loop(adapter, SessionManager(), acp, cfg, client)

    infos = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "info"]
    assert infos, "/state must produce an info reply"
    body = infos[0].detail or ""
    assert "day 7" in body
    assert "hazardous" in body
    assert "Science: 42" in body
    assert infos[0].dialog_id == "u1"


async def test_state_command_without_client_explains(cfg: ServeConfig) -> None:
    """`/state` when no game client was wired says so instead of going silent."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="/state", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    infos = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "info"]
    assert infos and "state unavailable" in (infos[0].detail or "")


async def test_active_state_change_includes_preview_when_client_present(cfg: ServeConfig) -> None:
    """Session-active replies carry a one-line game-state preview as detail."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="hi", chat_id=42)])
    acp = _FakeACP()

    class _FakeSummary:
        settlement = "Folktails"
        faction = "Folktails"
        science = 0
        time = type("T", (), {"dayNumber": 12, "dayProgress": 0.1, "speed": 1})()
        weather = type("W", (), {
            "cycle": None, "isHazardous": False,
            "hazardousWeatherDuration": None, "temperateWeatherDuration": None,
        })()
        districts = []
        alerts = None
        buildings = None

    client = MagicMock()
    client.summary = MagicMock(return_value=_FakeSummary())

    await _user_message_loop(adapter, SessionManager(), acp, cfg, client)

    actives = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "active"]
    assert actives
    assert "day 12" in (actives[0].detail or "")


async def test_first_prompt_carries_agent_spec_bootstrap(cfg: ServeConfig) -> None:
    """The very first turn of a new ACP session must be prefixed with the
    agent identity + tool scope. Subsequent turns must NOT re-inject it."""
    from timberbot.connector.agent_spec import TIMBERBOT_SPEC

    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="hi colony", chat_id=42),
        UserMessage(dialog_id="u1", text="and again", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    first, second = acp.session.prompts
    # First prompt: bootstrap + user text
    assert "TIMBERBOT_SYSTEM_BOUNDARY" in first
    assert TIMBERBOT_SPEC.refusal_sentence in first
    assert first.endswith("hi colony")
    # Second prompt: plain user text, no re-bootstrap
    assert "TIMBERBOT_SYSTEM_BOUNDARY" not in second
    assert second == "and again"


async def test_main_bootstrap_contains_delegation_block(cfg: ServeConfig) -> None:
    """Phase 1 §Delegation: the main bootstrap mentions the delegate-family tools."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="hi", chat_id=42)])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    first = acp.session.prompts[0]
    # The Delegation section lists subagent slugs and the MCP tool names.
    assert "Delegating to subagents" in first
    assert "mcp__game__delegate" in first
    assert "mcp__game__subagent_wait" in first


async def test_bootstrap_re_injected_after_session_eviction(cfg: ServeConfig) -> None:
    """After /halt evicts the session, the next turn opens a fresh ACP
    session — and that new session must again carry the bootstrap. (Pre-
    Phase-2 the same was true for /cancel; the soft semantic now keeps the
    session, so we use /halt to trigger the reconnect.)"""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="/halt", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    session1 = _FakeSession("acp-sess-1")
    session2 = _FakeSession("acp-sess-2")
    conn1 = _FakeConnection(session1)
    conn2 = _FakeConnection(session2)
    acp = MagicMock()
    acp.connect = AsyncMock(side_effect=[conn1, conn2])

    await _user_message_loop(adapter, SessionManager(), acp, ServeConfig(telegram_token="fake"))

    assert "TIMBERBOT_SYSTEM_BOUNDARY" in session1.prompts[0]
    assert "TIMBERBOT_SYSTEM_BOUNDARY" in session2.prompts[0]


async def test_reset_stream_called_before_each_prompt(cfg: ServeConfig) -> None:
    """Between user turns, the adapter's reset_stream is called so the next
    agent reply starts a fresh placeholder instead of editing the old one."""
    class _AdapterWithReset(_FakeAdapter):
        def __init__(self, inbound):
            super().__init__(inbound)
            self.resets: list[str] = []

        def reset_stream(self, session_id: str) -> None:
            self.resets.append(session_id)

    adapter = _AdapterWithReset([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    acp = _FakeACP()

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    # reset_stream should fire before each prompt dispatch — once per turn.
    assert adapter.resets == ["acp-sess-1", "acp-sess-1"]


async def test_prompt_error_emits_error_state_to_user(cfg: ServeConfig) -> None:
    """If session.prompt() raises, the user gets a SessionStateChange(state='error')."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="hi", chat_id=42)])
    session = _FakeSession()
    session.prompt = AsyncMock(side_effect=RuntimeError("agent crashed"))  # type: ignore[method-assign]
    conn = _FakeConnection(session)
    acp = MagicMock()
    acp.connect = AsyncMock(return_value=conn)

    await _user_message_loop(adapter, SessionManager(), acp, ServeConfig(telegram_token="fake"))

    errors = [m for m in adapter.sent if isinstance(m, SessionStateChange) and m.state == "error"]
    assert errors, "user must be told when the agent fails"
    assert "agent crashed" in (errors[0].detail or "")


class _RecordingBroker:
    """Test double for `SubagentBroker` — only records the surface
    `_user_message_loop` touches."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register(self, dialog_id, conn, agent_cwd, mcp_servers, **_):
        self.registered.append(dialog_id)

    async def unregister(self, dialog_id):
        self.unregistered.append(dialog_id)

    def get(self, dialog_id):
        # Loop calls this on `/cancel` to enumerate live subagents; the
        # recording fake has none so returning None is fine.
        return None


async def test_broker_registered_on_session_open_and_unregistered_on_halt(cfg: ServeConfig) -> None:
    """The broker tracks per-user (conn, registry) pairs — registered when
    the user's main session opens and unregistered when `/halt` tears the
    connection down. (Phase 2 soft `/cancel` keeps the registration.)"""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="/halt", chat_id=42),
    ])
    acp = _FakeACP()
    broker = _RecordingBroker()
    await _user_message_loop(adapter, SessionManager(), acp, cfg, broker=broker)

    assert broker.registered == ["u1"]
    assert broker.unregistered == ["u1"]


async def test_broker_not_re_registered_on_soft_cancel(cfg: ServeConfig) -> None:
    """Phase 2: `/cancel` must keep the broker registration alive — the
    user's subagents (and AgentConnection) survive across the cancel. The
    loop's normal teardown still unregisters at shutdown, so we observe
    the soft-cancel guarantee via the register count: exactly one
    registration covering both the pre-cancel and post-cancel turns."""
    adapter = _FakeAdapter([
        UserMessage(dialog_id="u1", text="first", chat_id=42),
        UserMessage(dialog_id="u1", text="/cancel", chat_id=42),
        UserMessage(dialog_id="u1", text="second", chat_id=42),
    ])
    acp = _FakeACP()
    broker = _RecordingBroker()
    await _user_message_loop(adapter, SessionManager(), acp, cfg, broker=broker)

    assert broker.registered == ["u1"], (
        f"soft /cancel must not re-register; got {broker.registered}"
    )
    # One unregister at loop teardown is expected; the assertion above is
    # what guarantees soft-cancel didn't trigger an eviction-and-reconnect cycle.
    assert broker.unregistered == ["u1"]


async def test_mcp_server_config_carries_dialog_id_header(cfg: ServeConfig) -> None:
    """`_user_message_loop` must thread `X-Timberbot-Dialog-Id: <dialog>`
    into the SSE MCP server config so the delegate-family tool handlers
    can route requests back to the right dialog."""
    adapter = _FakeAdapter([UserMessage(dialog_id="u1", text="hi", chat_id=42)])
    acp = _FakeACP()
    captured_mcp_servers: list[list[dict]] = []

    class _SessionCapturingConn(_FakeConnection):
        async def new_session(self, cwd, mcp_servers, allowed_tools=None):
            captured_mcp_servers.append(mcp_servers)
            return self.session

    acp.connection = _SessionCapturingConn(acp.session)

    await _user_message_loop(adapter, SessionManager(), acp, cfg)

    assert captured_mcp_servers, "new_session must have been called"
    headers = captured_mcp_servers[0][0]["headers"]
    assert {"name": "X-Timberbot-Dialog-Id", "value": "u1"} in headers
