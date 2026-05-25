from __future__ import annotations

import asyncio
import contextlib

import pytest

from timberbot.connector.connector import ACPConnector
from timberbot.connector.session import SessionHandle, SessionState


class FakeTransport:
    """Test double that pairs each outgoing request with a pre-configured response.

    recv_line blocks until send() is called (simulating that a real subprocess
    only responds after receiving a request). For server-initiated notifications,
    use push_notification() to inject a message outside the request/response cycle.

    Only request frames (those carrying an "id") consume a queued response;
    notifications (e.g. session/cancel) are recorded but don't pull one off the
    queue, mirroring real ACP framing.
    """

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.sent: list[dict] = []
        self.closed = False
        self._inbox: asyncio.Queue[dict | None] = asyncio.Queue()

    async def start(self) -> None:
        pass

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)
        # Deliver the next pre-configured response when a request is sent.
        # Outbound responses (our replies to server requests, which carry a
        # "result"/"error" instead of a "method") and notifications don't expect
        # an answer back.
        if "method" in msg and "id" in msg and self._responses:
            await self._inbox.put(self._responses.pop(0))

    async def recv_line(self) -> dict | None:
        return await self._inbox.get()

    async def close(self) -> None:
        self.closed = True

    async def push_notification(self, msg: dict) -> None:
        """Inject a server-initiated notification (not tied to a request)."""
        await self._inbox.put(msg)

    async def push_eof(self) -> None:
        """Signal EOF."""
        await self._inbox.put(None)


def _init_response() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"protocolVersion": 1, "agentCapabilities": {"mcpCapabilities": {"sse": True}}},
    }


async def _drain(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_initialize_transitions_to_active():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    assert handle.state == SessionState.ACTIVE
    assert handle.agent_capabilities == {"mcpCapabilities": {"sse": True}}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_initialize_sends_spec_capabilities():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    init = transport.sent[0]
    assert init["method"] == "initialize"
    assert init["params"]["protocolVersion"] == 1
    assert init["params"]["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_new_session_returns_session_id_and_resolves_cwd():
    transport = FakeTransport([
        _init_response(),
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "abc"}},
    ])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    sid = await handle.new_session(cwd="/tmp", mcp_servers=[])
    assert sid == "abc"
    assert handle.session_id == "abc"

    new_req = transport.sent[1]
    assert new_req["method"] == "session/new"
    # cwd must be an absolute path per spec.
    assert new_req["params"]["cwd"].startswith("/")
    # No model configured -> no session/set_model request.
    assert all(m.get("method") != "session/set_model" for m in transport.sent)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_new_session_pins_model_when_configured():
    transport = FakeTransport([
        _init_response(),
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "abc"}},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
    ])
    handle = SessionHandle(transport, model="claude-opus-4-7")
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    await handle.new_session(cwd="/tmp", mcp_servers=[])

    set_model = [m for m in transport.sent if m.get("method") == "session/set_model"]
    assert len(set_model) == 1
    assert set_model[0]["params"] == {"sessionId": "abc", "modelId": "claude-opus-4-7"}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_new_session_tolerates_set_model_error():
    """An agent without model selection answers session/set_model with an error;
    new_session must still succeed (soft failure)."""
    transport = FakeTransport([
        _init_response(),
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "abc"}},
        {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found"}},
    ])
    handle = SessionHandle(transport, model="glm-4.6")
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    sid = await handle.new_session(cwd="/tmp", mcp_servers=[])
    assert sid == "abc"
    assert handle.session_id == "abc"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_prompt_sends_content_block_list():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    await handle.prompt("abc", "summarize my colony")
    prompts = [m for m in transport.sent if m.get("method") == "session/prompt"]
    assert len(prompts) == 1
    assert prompts[0]["params"] == {
        "sessionId": "abc",
        "prompt": [{"type": "text", "text": "summarize my colony"}],
    }
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_prompt_does_not_block_on_turn_completion():
    """prompt() returns once the request is sent, before the turn's stopReason
    response arrives — so the caller stays free to dispatch /cancel."""
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    # No queued response for the prompt request; the call must still complete.
    await asyncio.wait_for(handle.prompt("abc", "hi"), timeout=1.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_session_update_fires_callback_with_text_block():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    received: list[tuple[str, str]] = []

    async def on_update(sid: str, chunk: str) -> None:
        received.append((sid, chunk))

    handle.on_update = on_update
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    notif = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "abc",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        },
    }
    await transport.push_notification(notif)
    await _drain()

    assert ("abc", "hello") in received
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_session_update_surfaces_thought_chunks():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    received: list[tuple[str, str]] = []

    async def on_update(sid: str, chunk: str) -> None:
        received.append((sid, chunk))

    handle.on_update = on_update
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    await transport.push_notification({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "abc",
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "thinking…"},
            },
        },
    })
    await _drain()

    assert ("abc", "thinking…") in received
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_session_update_skips_non_text_and_other_kinds():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    received: list[tuple[str, str]] = []

    async def on_update(sid: str, chunk: str) -> None:
        received.append((sid, chunk))

    handle.on_update = on_update
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    # Non-text content block on a message chunk — skipped.
    await transport.push_notification({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "abc",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "image", "data": "…", "mimeType": "image/png"},
            },
        },
    })
    # A tool_call update — not a text surface, skipped.
    await transport.push_notification({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "abc",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "mcp__game__summary",
            },
        },
    })
    await _drain()

    assert received == []
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _permission_request(title: str, kind: str = "other") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "session/request_permission",
        "params": {
            "sessionId": "abc",
            "toolCall": {"toolCallId": "t1", "title": title, "kind": kind, "rawInput": {}},
            "options": [
                {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_permission_auto_approved_for_allowed_tool():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport, allowed_tools=["game.*"])
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    # The Claude Agent SDK titles MCP tools mcp__<server>__<tool>.
    await transport.push_notification(_permission_request("mcp__game__summary"))
    await _drain()

    responses = [m for m in transport.sent if m.get("id") == 99]
    assert responses, "no response sent for permission request"
    assert responses[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "allow"}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_permission_auto_rejected_for_unknown_tool():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport, allowed_tools=["game.*"])
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    # A shell command (Bash) — title is the command, kind execute; not allowed.
    await transport.push_notification(_permission_request("rm -rf /", kind="execute"))
    await _drain()

    responses = [m for m in transport.sent if m.get("id") == 99]
    assert responses, "no response sent for permission request"
    assert responses[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "reject"}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_game_elicitation_fires_callback():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    received: list[tuple[str, dict]] = []

    async def on_elicitation(sid: str, payload: dict) -> None:
        received.append((sid, payload))

    handle.on_elicitation = on_elicitation
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    elicit_notif = {
        "jsonrpc": "2.0",
        "method": "game/elicitation",
        "params": {"sessionId": "abc", "question": "should I build?"},
    }
    await transport.push_notification(elicit_notif)
    await _drain()

    assert received, "on_elicitation was not called"
    assert received[0][0] == "abc"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_eof_transitions_to_ended():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    await transport.push_eof()
    await _drain(10)
    assert handle.state == SessionState.ENDED
    await task


@pytest.mark.asyncio
async def test_cancel_sends_notification_and_transitions_to_halting():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    await handle.cancel("abc")
    assert handle.state == SessionState.HALTING

    cancels = [m for m in transport.sent if m.get("method") == "session/cancel"]
    assert len(cancels) == 1
    # Notifications carry no id.
    assert "id" not in cancels[0]
    assert cancels[0]["params"] == {"sessionId": "abc"}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_close_cancels_read_task_and_pending_futures():
    transport = FakeTransport([_init_response()])
    handle = SessionHandle(transport)
    asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    await _drain()  # let read_loop register itself

    pending_fut: asyncio.Future = asyncio.get_running_loop().create_future()
    handle._pending[999] = pending_fut

    await handle.close()

    assert handle.state == SessionState.ENDED
    assert handle._read_task is None
    assert pending_fut.cancelled()
    assert 999 not in handle._pending
    assert transport.closed is True


@pytest.mark.asyncio
async def test_connect_returns_active_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """ACPConnector.connect() spawns transport, sends initialize, and returns an ACTIVE handle."""

    class _FakeAdapter:
        def build_argv(self, binary: str, model: str) -> list[str]:
            return [binary]

    class _FakeSubprocessTransport(FakeTransport):
        def __init__(self, argv: list[str], cwd: str | None = None) -> None:
            super().__init__([_init_response()])

    monkeypatch.setattr("timberbot.connector.connector.SubprocessTransport", _FakeSubprocessTransport)

    connector = ACPConnector(adapter=_FakeAdapter(), allowed_tools=["game.*"])
    handle = await connector.connect("claude-agent-acp", "claude-opus-4-7")

    assert handle.state == SessionState.ACTIVE
    await handle.close()
