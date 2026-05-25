"""Tests for the SDK-backed ACP connector.

The wire format itself is owned and validated by the `agent-client-protocol`
SDK, so these tests focus on Timberbot's own logic: how `SessionHandle`
orchestrates the connection, and how `_ConnectorClient` maps streaming updates
and permission requests onto Timberbot's callbacks and `allowed_tools`.
"""
from __future__ import annotations

import asyncio

import acp
import pytest
from acp import schema

from timberbot.connector.session import (
    SessionHandle,
    SessionState,
    _pick_option,
    _to_mcp_server,
    _tool_match_names,
)


class _FakeConn:
    """Stand-in for acp.ClientSideConnection that records the calls SessionHandle makes."""

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


def _handle_with_conn(*, model=None, allowed_tools=None, set_model_error=None) -> tuple[SessionHandle, _FakeConn]:
    handle = SessionHandle(allowed_tools=allowed_tools, model=model)
    conn = _FakeConn(set_model_error=set_model_error)
    handle._conn = conn
    return handle, conn


# --- SessionHandle orchestration ----------------------------------------


@pytest.mark.asyncio
async def test_initialize_sets_active_and_captures_caps():
    handle, conn = _handle_with_conn()
    await handle.initialize()
    assert handle.state == SessionState.ACTIVE
    assert isinstance(handle.agent_capabilities, schema.AgentCapabilities)
    assert conn.calls[0][0] == "initialize"
    assert conn.calls[0][1] == acp.PROTOCOL_VERSION
    caps = conn.calls[0][2]
    assert caps.terminal is False
    assert caps.fs.read_text_file is False and caps.fs.write_text_file is False


@pytest.mark.asyncio
async def test_new_session_resolves_cwd_and_converts_mcp_servers():
    handle, conn = _handle_with_conn()
    sid = await handle.new_session(
        cwd="/tmp",
        mcp_servers=[{"type": "sse", "name": "game", "url": "http://127.0.0.1:8091/sse", "headers": []}],
    )
    assert sid == "acp-1"
    assert handle.session_id == "acp-1"
    _, cwd, servers = next(c for c in conn.calls if c[0] == "new_session")
    assert cwd.startswith("/")  # absolute
    assert len(servers) == 1 and isinstance(servers[0], schema.SseMcpServer)
    assert servers[0].url.endswith("/sse")
    # No model configured -> no set_session_model.
    assert not any(c[0] == "set_session_model" for c in conn.calls)


@pytest.mark.asyncio
async def test_new_session_pins_model_when_configured():
    handle, conn = _handle_with_conn(model="claude-opus-4-7")
    await handle.new_session(cwd="/tmp", mcp_servers=[])
    set_model = [c for c in conn.calls if c[0] == "set_session_model"]
    assert set_model == [("set_session_model", "claude-opus-4-7", "acp-1")]


@pytest.mark.asyncio
async def test_new_session_tolerates_set_model_error():
    handle, conn = _handle_with_conn(model="glm-4.6", set_model_error=acp.RequestError.method_not_found("x"))
    sid = await handle.new_session(cwd="/tmp", mcp_servers=[])
    assert sid == "acp-1"  # soft failure: session still created
    assert any(c[0] == "set_session_model" for c in conn.calls)


@pytest.mark.asyncio
async def test_prompt_dispatches_text_block_without_blocking():
    handle, conn = _handle_with_conn()
    handle.session_id = "acp-1"
    # conn.prompt blocks until released; prompt() must return before that.
    await asyncio.wait_for(handle.prompt("acp-1", "summarize my colony"), timeout=1.0)
    await asyncio.wait_for(conn.prompt_started.wait(), timeout=1.0)
    _, sid, blocks = next(c for c in conn.calls if c[0] == "prompt")
    assert sid == "acp-1"
    assert len(blocks) == 1 and isinstance(blocks[0], schema.TextContentBlock)
    assert blocks[0].text == "summarize my colony"
    conn.prompt_release.set()
    await handle.close()


@pytest.mark.asyncio
async def test_cancel_calls_conn_and_transitions_to_halting():
    handle, conn = _handle_with_conn()
    await handle.cancel("acp-1")
    assert ("cancel", "acp-1") in conn.calls
    assert handle.state == SessionState.HALTING


@pytest.mark.asyncio
async def test_close_tears_down_connection_and_ends():
    handle, conn = _handle_with_conn()
    await handle.close()
    assert conn.closed is True
    assert handle.state == SessionState.ENDED


# --- _ConnectorClient: streaming updates --------------------------------


@pytest.mark.asyncio
async def test_session_update_forwards_message_chunk_text():
    handle, _ = _handle_with_conn()
    received: list[tuple[str, str]] = []

    async def on_update(sid, text):
        received.append((sid, text))

    handle.on_update = on_update
    update = schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block("hello"))
    await handle._client.session_update(session_id="acp-1", update=update)
    assert received == [("acp-1", "hello")]


@pytest.mark.asyncio
async def test_session_update_forwards_thought_chunk_text():
    handle, _ = _handle_with_conn()
    received: list[tuple[str, str]] = []

    async def on_update(sid, text):
        received.append((sid, text))

    handle.on_update = on_update
    update = schema.AgentThoughtChunk(session_update="agent_thought_chunk", content=acp.text_block("thinking"))
    await handle._client.session_update(session_id="acp-1", update=update)
    assert received == [("acp-1", "thinking")]


@pytest.mark.asyncio
async def test_session_update_skips_non_text_content():
    handle, _ = _handle_with_conn()
    received: list[tuple[str, str]] = []

    async def on_update(sid, text):
        received.append((sid, text))

    handle.on_update = on_update
    img = schema.ImageContentBlock(type="image", data="zzz", mime_type="image/png")
    update = schema.AgentMessageChunk(session_update="agent_message_chunk", content=img)
    await handle._client.session_update(session_id="acp-1", update=update)
    assert received == []


# --- _ConnectorClient: permission requests ------------------------------


def _options() -> list[schema.PermissionOption]:
    return [
        schema.PermissionOption(option_id="allow_always", name="Always", kind="allow_always"),
        schema.PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
        schema.PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
    ]


@pytest.mark.asyncio
async def test_permission_approves_allowed_mcp_tool():
    handle, _ = _handle_with_conn(allowed_tools=["game.*"])
    tool_call = schema.ToolCallUpdate(tool_call_id="t1", title="mcp__game__summary", kind="other")
    resp = await handle._client.request_permission(
        options=_options(), session_id="acp-1", tool_call=tool_call
    )
    assert isinstance(resp.outcome, schema.AllowedOutcome)
    assert resp.outcome.option_id == "allow"  # the allow_once option


@pytest.mark.asyncio
async def test_permission_rejects_unknown_tool():
    handle, _ = _handle_with_conn(allowed_tools=["game.*"])
    tool_call = schema.ToolCallUpdate(tool_call_id="t1", title="rm -rf /", kind="execute")
    resp = await handle._client.request_permission(
        options=_options(), session_id="acp-1", tool_call=tool_call
    )
    assert isinstance(resp.outcome, schema.AllowedOutcome)
    assert resp.outcome.option_id == "reject"  # the reject_once option


@pytest.mark.asyncio
async def test_permission_cancels_when_no_usable_option():
    handle, _ = _handle_with_conn(allowed_tools=["game.*"])
    tool_call = schema.ToolCallUpdate(tool_call_id="t1", title="mcp__game__summary", kind="other")
    resp = await handle._client.request_permission(options=[], session_id="acp-1", tool_call=tool_call)
    assert isinstance(resp.outcome, schema.DeniedOutcome)
    assert resp.outcome.outcome == "cancelled"


# --- _ConnectorClient: game elicitation extension -----------------------


@pytest.mark.asyncio
async def test_ext_notification_routes_game_elicitation():
    handle, _ = _handle_with_conn()
    received: list[tuple[str, dict]] = []

    async def on_elicitation(sid, payload):
        received.append((sid, payload))

    handle.on_elicitation = on_elicitation
    await handle._client.ext_notification("game/elicitation", {"sessionId": "acp-1", "question": "build?"})
    assert received and received[0][0] == "acp-1"


@pytest.mark.asyncio
async def test_ext_notification_ignores_other_methods():
    handle, _ = _handle_with_conn()
    received: list = []

    async def on_elicitation(sid, payload):
        received.append((sid, payload))

    handle.on_elicitation = on_elicitation
    await handle._client.ext_notification("some/other", {"sessionId": "acp-1"})
    assert received == []


# --- pure helpers --------------------------------------------------------


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
