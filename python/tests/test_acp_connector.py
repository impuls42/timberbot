"""Tests for the SDK-backed ACP connector.

The wire format itself is owned and validated by the `agent-client-protocol`
SDK, so these tests focus on Timberbot's own logic: how `AgentConnection`
orchestrates the subprocess + stdio, how `Session` drives one conversation
thread, and how `_ConnectorClient` maps streaming updates and permission
requests onto each session's callbacks and `allowed_tools`.
"""
from __future__ import annotations

import asyncio

import acp
import pytest
from acp import schema

from timberbot.connector.session import (
    AgentConnection,
    Session,
    SessionState,
    _clean_tool_title,
    _format_tool_input,
    _is_write_tool,
    _pick_option,
    _to_mcp_server,
    _tool_match_names,
)


class _FakeConn:
    """Stand-in for acp.ClientSideConnection that records the calls AgentConnection makes."""

    def __init__(self, *, set_model_error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.closed = False
        self._set_model_error = set_model_error
        self.prompt_started = asyncio.Event()
        self.prompt_release = asyncio.Event()

    async def initialize(self, protocol_version: int, client_capabilities=None, **_):
        self.calls.append(("initialize", protocol_version, client_capabilities))
        return schema.InitializeResponse(protocol_version=protocol_version,
                                         agent_capabilities=schema.AgentCapabilities())

    async def new_session(self, cwd: str, mcp_servers=None, **_):
        self.calls.append(("new_session", cwd, mcp_servers))
        return schema.NewSessionResponse(session_id="acp-1")

    async def set_session_model(self, model_id: str, session_id: str, **_):
        self.calls.append(("set_session_model", model_id, session_id))
        if self._set_model_error is not None:
            raise self._set_model_error
        return schema.SetSessionModelResponse()

    async def prompt(self, prompt, session_id: str, **_):
        self.calls.append(("prompt", session_id, prompt))
        self.prompt_started.set()
        await self.prompt_release.wait()  # hold the turn open until released
        return schema.PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **_):
        self.calls.append(("cancel", session_id))

    async def close(self):
        self.closed = True


def _conn_with_fake(*, model=None, set_model_error=None) -> tuple[AgentConnection, _FakeConn]:
    """Build an AgentConnection wired to a FakeConn — skips spawn/initialize plumbing."""
    conn = AgentConnection(model=model)
    fake = _FakeConn(set_model_error=set_model_error)
    conn._conn = fake
    return conn, fake


async def _conn_with_session(
    *, model=None, allowed_tools=None,
) -> tuple[AgentConnection, _FakeConn, Session]:
    conn, fake = _conn_with_fake(model=model)
    session = await conn.new_session(cwd="/tmp", mcp_servers=[], allowed_tools=allowed_tools)
    return conn, fake, session


# --- AgentConnection orchestration --------------------------------------


@pytest.mark.asyncio
async def test_initialize_captures_caps():
    conn, fake = _conn_with_fake()
    await conn.initialize()
    assert isinstance(conn.agent_capabilities, schema.AgentCapabilities)
    assert fake.calls[0][0] == "initialize"
    assert fake.calls[0][1] == acp.PROTOCOL_VERSION
    caps = fake.calls[0][2]
    assert caps.terminal is False
    assert caps.fs.read_text_file is False and caps.fs.write_text_file is False


@pytest.mark.asyncio
async def test_new_session_resolves_cwd_and_converts_mcp_servers():
    conn, fake = _conn_with_fake()
    session = await conn.new_session(
        cwd="/tmp",
        mcp_servers=[{"type": "sse", "name": "game", "url": "http://127.0.0.1:8091/sse", "headers": []}],
    )
    assert isinstance(session, Session)
    assert session.session_id == "acp-1"
    assert session.state == SessionState.ACTIVE
    assert conn._sessions["acp-1"] is session
    _, cwd, servers = next(c for c in fake.calls if c[0] == "new_session")
    assert cwd.startswith("/")  # absolute
    assert len(servers) == 1 and isinstance(servers[0], schema.SseMcpServer)
    assert servers[0].url.endswith("/sse")
    # No model configured -> no set_session_model.
    assert not any(c[0] == "set_session_model" for c in fake.calls)


@pytest.mark.asyncio
async def test_new_session_pins_model_when_configured():
    conn, fake = _conn_with_fake(model="claude-opus-4-7")
    await conn.new_session(cwd="/tmp", mcp_servers=[])
    set_model = [c for c in fake.calls if c[0] == "set_session_model"]
    assert set_model == [("set_session_model", "claude-opus-4-7", "acp-1")]


@pytest.mark.asyncio
async def test_new_session_tolerates_set_model_error():
    conn, fake = _conn_with_fake(
        model="glm-4.6",
        set_model_error=acp.RequestError.method_not_found("x"),
    )
    session = await conn.new_session(cwd="/tmp", mcp_servers=[])
    assert session.session_id == "acp-1"  # soft failure: session still created
    assert any(c[0] == "set_session_model" for c in fake.calls)


@pytest.mark.asyncio
async def test_multiple_sessions_on_one_connection():
    """Open two sessions; both should be registered and have distinct ids."""
    conn = AgentConnection()

    class _MultiSessionConn(_FakeConn):
        _counter = 0

        async def new_session(self, cwd: str, mcp_servers=None, **_):
            self._counter += 1
            return schema.NewSessionResponse(session_id=f"acp-{self._counter}")

    fake = _MultiSessionConn()
    conn._conn = fake
    s1 = await conn.new_session(cwd="/tmp", mcp_servers=[])
    s2 = await conn.new_session(cwd="/tmp", mcp_servers=[])
    assert s1.session_id == "acp-1"
    assert s2.session_id == "acp-2"
    assert set(conn._sessions.keys()) == {"acp-1", "acp-2"}


# --- Session: prompt/cancel/close ---------------------------------------


@pytest.mark.asyncio
async def test_session_prompt_is_fire_and_forget():
    conn, fake, session = await _conn_with_session()
    # fake.prompt blocks until released; session.prompt() must return before that.
    await asyncio.wait_for(session.prompt("summarize my colony"), timeout=1.0)
    await asyncio.wait_for(fake.prompt_started.wait(), timeout=1.0)
    _, sid, blocks = next(c for c in fake.calls if c[0] == "prompt")
    assert sid == "acp-1"
    assert len(blocks) == 1 and isinstance(blocks[0], schema.TextContentBlock)
    assert blocks[0].text == "summarize my colony"
    fake.prompt_release.set()
    await conn.close()


@pytest.mark.asyncio
async def test_session_prompt_awaitable_returns_full_reply():
    """prompt_awaitable accumulates streamed chunks and resolves with the joined text."""
    conn, fake, session = await _conn_with_session()
    captured: list[str] = []
    async def on_update(sid, text):
        captured.append(text)
    session.on_update = on_update

    awaitable = asyncio.create_task(session.prompt_awaitable("hi"))
    await fake.prompt_started.wait()
    # Stream two chunks via the connector client while the turn is in flight.
    chunk1 = schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block("Hel"))
    chunk2 = schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block("lo!"))
    await conn._client.session_update(session_id="acp-1", update=chunk1)
    await conn._client.session_update(session_id="acp-1", update=chunk2)
    # Now end the turn so the awaiter resolves.
    fake.prompt_release.set()
    reply = await asyncio.wait_for(awaitable, timeout=1.0)
    assert reply == "Hello!"
    assert captured == ["Hel", "lo!"]
    assert session.current_stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_session_prompt_awaitable_busy_rejects():
    conn, fake, session = await _conn_with_session()
    # Hold the turn open by NOT setting prompt_release.
    first = asyncio.create_task(session.prompt_awaitable("first"))
    await fake.prompt_started.wait()
    with pytest.raises(RuntimeError, match="busy"):
        await session.prompt_awaitable("second")
    fake.prompt_release.set()
    await first


@pytest.mark.asyncio
async def test_cancel_calls_conn_and_transitions_to_halting():
    conn, fake, session = await _conn_with_session()
    await session.cancel()
    assert ("cancel", "acp-1") in fake.calls
    assert session.state == SessionState.HALTING


@pytest.mark.asyncio
async def test_connection_close_ends_all_sessions():
    conn, fake, session = await _conn_with_session()
    await conn.close()
    assert fake.closed is True
    assert session.state == SessionState.ENDED
    assert session.session_id not in conn._sessions


# --- _ConnectorClient: streaming updates --------------------------------


@pytest.mark.asyncio
async def test_session_update_forwards_message_chunk_text():
    conn, _, session = await _conn_with_session()
    received: list[tuple[str, str]] = []

    async def on_update(sid, text):
        received.append((sid, text))

    session.on_update = on_update
    update = schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block("hello"))
    await conn._client.session_update(session_id="acp-1", update=update)
    assert received == [("acp-1", "hello")]


@pytest.mark.asyncio
async def test_session_update_forwards_thought_chunk_text():
    conn, _, session = await _conn_with_session()
    received: list[tuple[str, str]] = []

    async def on_update(sid, text):
        received.append((sid, text))

    session.on_update = on_update
    update = schema.AgentThoughtChunk(session_update="agent_thought_chunk", content=acp.text_block("thinking"))
    await conn._client.session_update(session_id="acp-1", update=update)
    assert received == [("acp-1", "thinking")]


@pytest.mark.asyncio
async def test_session_update_skips_non_text_content():
    conn, _, session = await _conn_with_session()
    received: list[tuple[str, str]] = []

    async def on_update(sid, text):
        received.append((sid, text))

    session.on_update = on_update
    img = schema.ImageContentBlock(type="image", data="zzz", mime_type="image/png")
    update = schema.AgentMessageChunk(session_update="agent_message_chunk", content=img)
    await conn._client.session_update(session_id="acp-1", update=update)
    assert received == []


@pytest.mark.asyncio
async def test_session_update_for_unknown_session_is_silent():
    """An update for a session_id that's not in `_sessions` should be a no-op,
    not a crash. This can happen briefly during teardown."""
    conn, _ = _conn_with_fake()
    update = schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block("orphan"))
    await conn._client.session_update(session_id="never-registered", update=update)


# --- _ConnectorClient: permission requests ------------------------------


def _options() -> list[schema.PermissionOption]:
    return [
        schema.PermissionOption(option_id="allow_always", name="Always", kind="allow_always"),
        schema.PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        schema.PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
    ]


@pytest.mark.asyncio
async def test_permission_approves_allowed_mcp_tool():
    conn, _, _ = await _conn_with_session(allowed_tools=["game.*"])
    tool_call = schema.ToolCallUpdate(tool_call_id="t1", title="mcp__game__summary", kind="other")
    resp = await conn._client.request_permission(
        options=_options(), session_id="acp-1", tool_call=tool_call,
    )
    assert isinstance(resp.outcome, schema.AllowedOutcome)
    assert resp.outcome.option_id == "allow"  # the allow_once option


@pytest.mark.asyncio
async def test_permission_rejects_unknown_tool():
    conn, _, _ = await _conn_with_session(allowed_tools=["game.*"])
    tool_call = schema.ToolCallUpdate(tool_call_id="t1", title="rm -rf /", kind="execute")
    resp = await conn._client.request_permission(
        options=_options(), session_id="acp-1", tool_call=tool_call,
    )
    assert isinstance(resp.outcome, schema.AllowedOutcome)
    assert resp.outcome.option_id == "reject"  # the reject_once option


@pytest.mark.asyncio
async def test_permission_cancels_when_no_usable_option():
    conn, _, _ = await _conn_with_session(allowed_tools=["game.*"])
    tool_call = schema.ToolCallUpdate(tool_call_id="t1", title="mcp__game__summary", kind="other")
    resp = await conn._client.request_permission(options=[], session_id="acp-1", tool_call=tool_call)
    assert isinstance(resp.outcome, schema.DeniedOutcome)
    assert resp.outcome.outcome == "cancelled"


@pytest.mark.asyncio
async def test_permission_enforced_per_session():
    """Two sessions on one connection can have different allowlists.

    Subagent sessions get narrower scope; this is the core guarantee Phase 1
    needs to make subagents safer than the main agent.
    """
    conn = AgentConnection()

    class _C(_FakeConn):
        _i = 0

        async def new_session(self, cwd, mcp_servers=None, **_):
            self._i += 1
            return schema.NewSessionResponse(session_id=f"acp-{self._i}")

    conn._conn = _C()
    s_main = await conn.new_session(cwd="/tmp", mcp_servers=[], allowed_tools=["game.*"])
    s_scout = await conn.new_session(
        cwd="/tmp", mcp_servers=[], allowed_tools=["game.find_placement", "game.summary"],
    )
    place = schema.ToolCallUpdate(tool_call_id="t1", title="mcp__game__place_building", kind="other")
    # Main: allowed.
    resp = await conn._client.request_permission(
        options=_options(), session_id=s_main.session_id, tool_call=place,
    )
    assert resp.outcome.option_id == "allow"
    # Scout: rejected — place_building is outside its allowlist.
    resp = await conn._client.request_permission(
        options=_options(), session_id=s_scout.session_id, tool_call=place,
    )
    assert resp.outcome.option_id == "reject"


# --- _ConnectorClient: game elicitation extension -----------------------


@pytest.mark.asyncio
async def test_ext_notification_routes_game_elicitation():
    conn, _, session = await _conn_with_session()
    received: list[tuple[str, dict]] = []

    async def on_elicitation(sid, payload):
        received.append((sid, payload))

    session.on_elicitation = on_elicitation
    await conn._client.ext_notification("game/elicitation", {"sessionId": "acp-1", "question": "build?"})
    assert received and received[0][0] == "acp-1"


@pytest.mark.asyncio
async def test_ext_notification_ignores_other_methods():
    conn, _, session = await _conn_with_session()
    received: list = []

    async def on_elicitation(sid, payload):
        received.append((sid, payload))

    session.on_elicitation = on_elicitation
    await conn._client.ext_notification("some/other", {"sessionId": "acp-1"})
    assert received == []


# --- pure helpers --------------------------------------------------------


def test_clean_tool_title_strips_mcp_framing():
    assert _clean_tool_title("mcp__game__place_building") == "place_building"
    assert _clean_tool_title("place_building") == "place_building"
    assert _clean_tool_title("Read") == "Read"  # pass-through


def test_is_write_tool_classification():
    assert _is_write_tool("place_building")
    assert _is_write_tool("set_recipe")
    assert _is_write_tool("demolish_crop")
    assert _is_write_tool("unlock_building")
    assert _is_write_tool("link")
    # Reads should not match:
    assert not _is_write_tool("summary")
    assert not _is_write_tool("alerts")
    assert not _is_write_tool("prefabs")
    assert not _is_write_tool("brain")


def test_format_tool_input_dict_and_overflow():
    out = _format_tool_input({"prefab": "LogPile", "x": 50, "y": 40, "z": 4})
    assert "prefab=LogPile" in out and "x=50" in out
    # Long values get truncated
    long_val = _format_tool_input({"data": "x" * 200})
    assert "…" in long_val
    # Too many keys get an ellipsis sentinel
    many = _format_tool_input({f"k{i}": i for i in range(10)})
    assert "…" in many


@pytest.mark.asyncio
async def test_session_update_emits_tool_action_on_completed_write():
    conn, _, session = await _conn_with_session()
    received: list[tuple[str, str, bool]] = []

    async def on_tool_action(sid: str, summary: str, ok: bool) -> None:
        received.append((sid, summary, ok))

    session.on_tool_action = on_tool_action
    update = schema.ToolCallStart(
        tool_call_id="t1",
        title="mcp__game__place_building",
        kind="other",
        status="completed",
        raw_input={"prefab": "LogPile", "x": 50, "y": 40, "z": 4},
        sessionUpdate="tool_call",
    )
    await conn._client.session_update("acp-1", update)
    assert len(received) == 1
    sid, summary, ok = received[0]
    assert sid == "acp-1"
    assert ok is True
    assert "place_building" in summary
    assert "prefab=LogPile" in summary


@pytest.mark.asyncio
async def test_session_update_resolves_title_from_earlier_start():
    """The Claude SDK emits Start (with title) then a separate Progress
    update with status=completed but title=None. The terminal update must
    still resolve to the right tool name from the earlier Start payload."""
    conn, _, session = await _conn_with_session()
    received: list = []

    async def on_tool_action(sid: str, summary: str, ok: bool) -> None:
        received.append(summary)

    session.on_tool_action = on_tool_action
    start = schema.ToolCallStart(
        tool_call_id="t9", title="mcp__game__place_building",
        kind="other", status="pending",
        raw_input={"prefab": "WaterPump", "x": 50},
        sessionUpdate="tool_call",
    )
    progress = schema.ToolCallProgress(
        tool_call_id="t9", title=None, status="completed",
        sessionUpdate="tool_call_update",
    )
    await conn._client.session_update("acp-1", start)
    await conn._client.session_update("acp-1", progress)
    assert len(received) == 1
    assert "place_building" in received[0]
    assert "prefab=WaterPump" in received[0]


@pytest.mark.asyncio
async def test_session_update_dedupes_tool_action_by_id():
    conn, _, session = await _conn_with_session()
    received: list = []

    async def on_tool_action(sid: str, summary: str, ok: bool) -> None:
        received.append(summary)

    session.on_tool_action = on_tool_action
    base = dict(tool_call_id="t1", title="mcp__game__set_recipe", kind="other", status="completed")
    await conn._client.session_update(
        "acp-1", schema.ToolCallStart(**base, sessionUpdate="tool_call"),
    )
    await conn._client.session_update(
        "acp-1", schema.ToolCallProgress(**base, sessionUpdate="tool_call_update"),
    )
    assert len(received) == 1


@pytest.mark.asyncio
async def test_session_update_skips_read_tools():
    """Read-only inspections (summary, alerts, etc.) must not spam the chat."""
    conn, _, session = await _conn_with_session()
    received: list = []

    async def on_tool_action(sid: str, summary: str, ok: bool) -> None:
        received.append(summary)

    session.on_tool_action = on_tool_action
    update = schema.ToolCallStart(
        tool_call_id="t2", title="mcp__game__summary", kind="other", status="completed",
        sessionUpdate="tool_call",
    )
    await conn._client.session_update("acp-1", update)
    assert received == []


@pytest.mark.asyncio
async def test_session_update_marks_failed_write_tool():
    conn, _, session = await _conn_with_session()
    received: list[tuple[str, bool]] = []

    async def on_tool_action(sid: str, summary: str, ok: bool) -> None:
        received.append((summary, ok))

    session.on_tool_action = on_tool_action
    update = schema.ToolCallStart(
        tool_call_id="t3", title="mcp__game__demolish_building", kind="other",
        status="failed", raw_input={"id": 17}, sessionUpdate="tool_call",
    )
    await conn._client.session_update("acp-1", update)
    assert len(received) == 1
    summary, ok = received[0]
    assert ok is False
    assert "demolish_building" in summary
    assert summary.startswith("❌")


def test_tool_match_names_normalizes_mcp_naming():
    tc = schema.ToolCallUpdate(tool_call_id="t1", title="mcp__game__place_building", kind="other")
    names = _tool_match_names(tc)
    assert "mcp__game__place_building" in names
    assert "game.place_building" in names


def test_tool_match_names_plain_title():
    tc = schema.ToolCallUpdate(tool_call_id="t1", title="Read foo.py", kind="read")
    assert _tool_match_names(tc) == {"Read foo.py"}


def test_pick_option_prefers_once_variants():
    assert _pick_option(_options(), approve=True) == "allow"
    assert _pick_option(_options(), approve=False) == "reject"


def test_pick_option_returns_none_when_empty():
    assert _pick_option([], approve=True) is None


def test_to_mcp_server_sse_and_http():
    sse = _to_mcp_server({"type": "sse", "name": "game", "url": "http://h/sse", "headers": []})
    assert isinstance(sse, schema.SseMcpServer) and sse.type == "sse"
    http = _to_mcp_server({"type": "http", "name": "game", "url": "http://h/mcp", "headers": []})
    assert isinstance(http, schema.HttpMcpServer) and http.type == "http"
