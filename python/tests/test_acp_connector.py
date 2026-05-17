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
        if self._responses:
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


async def _drain(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_initialize_transitions_to_active():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    assert handle.state == SessionState.ACTIVE
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_new_session_returns_session_id():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "abc"}},
    ])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    sid = await handle.new_session(cwd="/tmp", mcp_servers=[])
    assert sid == "abc"
    assert handle.session_id == "abc"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_session_update_fires_callback():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
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
        "params": {"sessionId": "abc", "chunk": "hello"},
    }
    await transport.push_notification(notif)
    await _drain()

    assert ("abc", "hello") in received
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_permission_auto_approved_for_allowed_tool():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
    handle = SessionHandle(transport, allowed_tools=["game.*"])
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    perm_notif = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "session/requestPermission",
        "params": {"toolName": "game.summary", "sessionId": "abc"},
    }
    await transport.push_notification(perm_notif)
    await _drain()

    permission_responses = [m for m in transport.sent if m.get("id") == 99]
    assert permission_responses, "no response sent for permission request"
    assert permission_responses[0]["result"]["approved"] is True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_permission_auto_rejected_for_unknown_tool():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
    handle = SessionHandle(transport, allowed_tools=["game.*"])
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()

    perm_notif = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "session/requestPermission",
        "params": {"toolName": "shell.exec", "sessionId": "abc"},
    }
    await transport.push_notification(perm_notif)
    await _drain()

    permission_responses = [m for m in transport.sent if m.get("id") == 99]
    assert permission_responses, "no response sent for permission request"
    assert permission_responses[0]["result"]["approved"] is False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_game_elicitation_fires_callback():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
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
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    await transport.push_eof()
    await _drain(10)
    assert handle.state == SessionState.ENDED
    await task


@pytest.mark.asyncio
async def test_cancel_transitions_to_halting():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
    ])
    handle = SessionHandle(transport)
    task = asyncio.get_running_loop().create_task(handle.read_loop())
    await handle.initialize()
    await handle.cancel("abc")
    assert handle.state == SessionState.HALTING
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_close_cancels_read_task_and_pending_futures():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
    ])
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
            return [binary, "--model", model]

    class _FakeSubprocessTransport(FakeTransport):
        def __init__(self, argv: list[str], cwd: str | None = None) -> None:
            super().__init__([
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2026-05"}},
            ])

    monkeypatch.setattr("timberbot.connector.connector.SubprocessTransport", _FakeSubprocessTransport)

    connector = ACPConnector(adapter=_FakeAdapter(), allowed_tools=["game.*"])
    handle = await connector.connect("claude", "claude-opus-4-7")

    assert handle.state == SessionState.ACTIVE
    await handle.close()
