"""Tests for `TimberbotWsClient` — the async WS wrapper.

Strategy: stand up an `aiohttp.web.Application` exposing a `/ws` handler as
the stand-in mod, wrap it in `aiohttp.test_utils.TestServer`, and exercise
the client against that real-but-in-process server. No dependency on the
actual C# mod at test time.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

pytest.importorskip("aiohttp")
# `pytest-asyncio` is a hard dev dependency and `asyncio_mode = "auto"` in
# pyproject.toml guarantees it's loaded — no importorskip needed.

import aiohttp  # noqa: E402
from aiohttp import WSMsgType, web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from timberbot.api.models.ws import EventPush, StatePush, WsMessage  # noqa: E402
from timberbot.api.wsclient import TimberbotWsClient  # noqa: E402

# Reference AgentState payload that satisfies the existing Pydantic model.
SAMPLE_AGENT_STATE = {
    "mode": "request",
    "goal": "build a sawmill",
    "ready": False,
    "pendingRequest": None,
    "agentStatus": "idle",
    "lastError": None,
}

AUTH_TOKEN = "shh-secret"


# ---------------------------------------------------------------------------
# Test server helpers
# ---------------------------------------------------------------------------


WsHandler = Callable[[web.WebSocketResponse, web.Request], Awaitable[None]]


def _build_app(
    *,
    handler: WsHandler,
    require_token: bool = False,
    accept_query_token: bool = False,
) -> web.Application:
    """Return an aiohttp app exposing `/api/ws`.

    `handler` runs after the upgrade. `require_token` toggles bearer-token
    auth (rejecting with 401 when missing/wrong). `accept_query_token` also
    accepts `?token=…` as a fallback.
    """
    async def ws_route(request: web.Request) -> web.WebSocketResponse:
        if require_token:
            header = request.headers.get("Authorization", "")
            query_token = request.query.get("token")
            token = ""
            if header.startswith("Bearer "):
                token = header[len("Bearer "):]
            elif accept_query_token and query_token:
                token = query_token
            if token != AUTH_TOKEN:
                # Reject the upgrade with a real 401 so the client surfaces
                # `WSServerHandshakeError`.
                return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await handler(ws, request)
        return ws

    app = web.Application()
    app.router.add_get("/api/ws", ws_route)
    return app


@pytest.fixture(autouse=True)
def _isolate_auth_env(monkeypatch):
    """Stop `TBOT_AUTH_TOKEN` (and user-config fallback) from leaking into
    tests that explicitly pass `auth_token=None`. `resolve_auth_token` walks
    the env + user config when its arg is empty, so we have to neuter both.
    """
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "timberbot.settings.client_config",
        lambda *_a, **_k: {},
    )


@pytest.fixture
async def server_factory() -> AsyncIterator[Callable[..., Awaitable[TestServer]]]:
    """Yield a builder that starts an aiohttp TestServer and tears it down."""
    started: list[TestServer] = []

    async def _build(**kwargs) -> TestServer:
        app = _build_app(**kwargs)
        srv = TestServer(app)
        await srv.start_server()
        started.append(srv)
        return srv

    yield _build

    for s in started:
        await s.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_handshake_with_token_succeeds(server_factory):
    """A correct Bearer token is accepted; the client can receive a state push."""
    seen: list[dict] = []

    async def handler(ws: web.WebSocketResponse, request: web.Request) -> None:
        seen.append(dict(request.headers))
        await ws.send_str(json.dumps({"type": "state", "payload": SAMPLE_AGENT_STATE}))
        await ws.close()

    srv = await server_factory(handler=handler, require_token=True)
    client = TimberbotWsClient(srv.host, srv.port, auth_token=AUTH_TOKEN)
    await client.connect()
    try:
        msg = await asyncio.wait_for(_next_msg(client), timeout=2)
    finally:
        await client.close()

    assert msg.type == "state"
    assert isinstance(msg.payload, StatePush)
    assert msg.payload.goal == "build a sawmill"
    assert seen[0].get("Authorization") == f"Bearer {AUTH_TOKEN}"


async def test_handshake_without_token_rejected_401(server_factory):
    """Missing token → server returns 401 → aiohttp raises WSServerHandshakeError."""
    async def handler(ws: web.WebSocketResponse, request: web.Request) -> None:
        # Should never run — auth rejects before upgrade.
        await ws.close()

    srv = await server_factory(handler=handler, require_token=True)
    client = TimberbotWsClient(srv.host, srv.port, auth_token=None)
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError) as ei:
            await client.connect()
        assert ei.value.status == 401
    finally:
        await client.close()


async def test_round_trip_envelope(server_factory):
    """Client → server send_message arrives with the expected `{type, payload}` shape."""
    received: list[dict] = []
    done = asyncio.Event()

    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                received.append(json.loads(msg.data))
                done.set()
                break
        await ws.close()

    srv = await server_factory(handler=handler)
    client = TimberbotWsClient(srv.host, srv.port)
    await client.connect()
    try:
        await client.send_message("heartbeat", {
            "version": "0.9.0",
            "agent_status": "idle",
            "acked_request_id": 7,
        })
        await asyncio.wait_for(done.wait(), timeout=2)
    finally:
        await client.close()

    assert received == [{
        "type": "heartbeat",
        "payload": {
            "version": "0.9.0",
            "agent_status": "idle",
            "acked_request_id": 7,
        },
    }]


async def test_reconnect_on_close(server_factory):
    """After the server drops the socket, the client transparently reconnects."""
    call_count = 0

    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        nonlocal call_count
        call_count += 1
        # First connection sends one frame then closes immediately; second
        # connection sends a distinguishing frame and stays open until we close.
        if call_count == 1:
            await ws.send_str(json.dumps({
                "type": "event",
                "payload": {"event": "first", "day": 1, "timestamp": 1, "data": None},
            }))
            await ws.close()
        else:
            await ws.send_str(json.dumps({
                "type": "event",
                "payload": {"event": "second", "day": 2, "timestamp": 2, "data": None},
            }))
            # Keep socket alive until client closes.
            async for _ in ws:
                pass

    srv = await server_factory(handler=handler)
    # Short backoff so the test runs quickly.
    client = TimberbotWsClient(srv.host, srv.port,
                               backoff_base=0.01, backoff_cap=0.05)
    await client.connect()
    try:
        first = await asyncio.wait_for(_next_msg(client), timeout=2)
        second = await asyncio.wait_for(_next_msg(client), timeout=3)
    finally:
        await client.close()

    assert isinstance(first.payload, EventPush) and first.payload.event == "first"
    assert isinstance(second.payload, EventPush) and second.payload.event == "second"
    assert call_count == 2


async def test_drops_bad_json_without_crashing(server_factory):
    """A malformed frame is logged + dropped; the next valid frame still yields."""
    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        # Garbage frame, then a valid frame.
        await ws.send_str("not json at all }{")
        await ws.send_str(json.dumps({
            "type": "event",
            "payload": {"event": "after_bad", "day": 5, "timestamp": 99, "data": None},
        }))
        # Keep open.
        async for _ in ws:
            pass

    srv = await server_factory(handler=handler)
    client = TimberbotWsClient(srv.host, srv.port,
                               backoff_base=0.01, backoff_cap=0.05)
    await client.connect()
    try:
        msg = await asyncio.wait_for(_next_msg(client), timeout=2)
    finally:
        await client.close()

    assert isinstance(msg.payload, EventPush)
    assert msg.payload.event == "after_bad"


async def test_drops_envelope_missing_type(server_factory):
    """Frame without a `type` field is dropped, not yielded."""
    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        await ws.send_str(json.dumps({"payload": {"hello": "world"}}))
        await ws.send_str(json.dumps({"type": "event",
                                      "payload": {"event": "ok", "day": 0,
                                                  "timestamp": 0, "data": None}}))
        async for _ in ws:
            pass

    srv = await server_factory(handler=handler)
    client = TimberbotWsClient(srv.host, srv.port,
                               backoff_base=0.01, backoff_cap=0.05)
    await client.connect()
    try:
        msg = await asyncio.wait_for(_next_msg(client), timeout=2)
    finally:
        await client.close()

    assert msg.type == "event"


async def test_unknown_message_type_passes_through(server_factory):
    """Forward-compat: an unrecognized `type` yields the envelope with raw payload."""
    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        await ws.send_str(json.dumps({"type": "future_thing",
                                      "payload": {"anything": [1, 2, 3]}}))
        async for _ in ws:
            pass

    srv = await server_factory(handler=handler)
    client = TimberbotWsClient(srv.host, srv.port,
                               backoff_base=0.01, backoff_cap=0.05)
    await client.connect()
    try:
        msg = await asyncio.wait_for(_next_msg(client), timeout=2)
    finally:
        await client.close()

    assert msg.type == "future_thing"
    assert msg.payload == {"anything": [1, 2, 3]}


async def test_query_token_fallback_used_when_enabled(server_factory):
    """With `query_token_fallback=True`, the URL carries `?token=…` and the
    server accepts the upgrade even though we strip the Authorization header.
    """
    seen: list[dict] = []

    async def handler(ws: web.WebSocketResponse, request: web.Request) -> None:
        seen.append({"auth_header": request.headers.get("Authorization"),
                     "query_token": request.query.get("token")})
        await ws.close()

    srv = await server_factory(handler=handler,
                               require_token=True, accept_query_token=True)
    client = TimberbotWsClient(srv.host, srv.port, auth_token=AUTH_TOKEN,
                                query_token_fallback=True)
    assert f"?token={AUTH_TOKEN}" in client.url
    await client.connect()
    await client.close()

    assert seen[0]["query_token"] == AUTH_TOKEN


def test_safe_url_redacts_query_token():
    """`safe_url` must not contain the auth token even when the query-token
    fallback is active — this is what we log.
    """
    client = TimberbotWsClient("h", 1, auth_token="sekret",
                               query_token_fallback=True)
    assert "sekret" in client.url
    assert "sekret" not in client.safe_url
    assert "?token=***" in client.safe_url

    # When the fallback is OFF, both URLs match (no query string at all).
    client2 = TimberbotWsClient("h", 1, auth_token="sekret",
                                query_token_fallback=False)
    assert client2.url == client2.safe_url
    assert "sekret" not in client2.safe_url


async def test_messages_survives_transient_handshake_failure(server_factory):
    """A non-401 handshake failure during reconnect must NOT terminate the
    `messages()` iterator — it should sleep + retry until the server is
    healthy again, then resume yielding frames.

    This regression-tests issue #4 from the round-1 review: previously, a
    failed redial left `_ws = None`, which caused `messages()` to break out
    of its loop silently.
    """
    call_count = 0

    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First connection sends a frame, then drops the socket.
            await ws.send_str(json.dumps({
                "type": "event",
                "payload": {"event": "before", "day": 1, "timestamp": 1, "data": None},
            }))
            await ws.close()
        else:
            # Subsequent connections: send a distinguishing frame, stay open.
            await ws.send_str(json.dumps({
                "type": "event",
                "payload": {"event": "after_retry", "day": 2,
                            "timestamp": 2, "data": None},
            }))
            async for _ in ws:
                pass

    srv = await server_factory(handler=handler)
    client = TimberbotWsClient(srv.host, srv.port,
                               backoff_base=0.01, backoff_cap=0.05)
    await client.connect()

    # Simulate a transient redial failure: monkey-patch `_dial` to raise on
    # the *first* reconnect attempt, then restore real behavior.
    real_dial = client._dial
    attempts = {"n": 0}

    async def flaky_dial():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise aiohttp.ClientError("simulated transient redial failure")
        return await real_dial()

    client._dial = flaky_dial  # type: ignore[assignment]

    try:
        first = await asyncio.wait_for(_next_msg_keep_open(client), timeout=2)
        second = await asyncio.wait_for(_next_msg_keep_open(client), timeout=3)
    finally:
        await client.close()

    assert isinstance(first.payload, EventPush)
    assert first.payload.event == "before"
    assert isinstance(second.payload, EventPush)
    assert second.payload.event == "after_retry"
    # We expect: 1 server connect, 1 failed redial, 1 successful redial.
    assert attempts["n"] >= 2
    assert call_count == 2


async def test_messages_survives_initial_dial_failure(server_factory):
    """A connection-refused at the very first `messages()` iteration must
    NOT propagate — it should fall through to the same backoff/retry path
    that mid-stream disconnects use.

    Regression: when `tbot serve` started before the mod was launched, the
    ingestor's `async for msg in ws_client.messages()` raised
    `ClientConnectorError` immediately, killing the TaskGroup and dumping
    a 100-line traceback to the user.
    """
    call_count = 0

    async def handler(ws: web.WebSocketResponse, _request: web.Request) -> None:
        nonlocal call_count
        call_count += 1
        await ws.send_str(json.dumps({
            "type": "event",
            "payload": {"event": "hello", "day": 1, "timestamp": 1, "data": None},
        }))
        async for _ in ws:
            pass

    srv = await server_factory(handler=handler)
    client = TimberbotWsClient(srv.host, srv.port,
                               backoff_base=0.01, backoff_cap=0.05)

    # Crucially, do NOT call connect() — drive messages() directly so the
    # initial dial happens inside the iterator. Then simulate one initial
    # failure before letting the real dial succeed.
    real_dial = client._dial
    attempts = {"n": 0}

    async def flaky_dial():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise aiohttp.ClientConnectorError(
                connection_key=type("K", (), {"ssl": False, "host": srv.host,
                                              "port": srv.port, "is_ssl": False})(),
                os_error=ConnectionRefusedError(111, "Connection refused"),
            )
        return await real_dial()

    client._dial = flaky_dial  # type: ignore[assignment]

    try:
        msg = await asyncio.wait_for(_next_msg_keep_open(client), timeout=3)
    finally:
        await client.close()

    assert isinstance(msg.payload, EventPush)
    assert msg.payload.event == "hello"
    # We expect: 1 failed initial dial, 1 successful retry via _reconnect.
    assert attempts["n"] >= 2


def test_exp_backoff_imported_from_utils():
    """The `exp_backoff` helper must live in `timberbot.utils` (so `api/`
    doesn't depend on `cli/commands/`). Both the WS client and the watch
    command must use the SAME function object.
    """
    from timberbot.api import wsclient as ws_mod
    from timberbot.cli.commands import watch as watch_mod
    from timberbot.utils import exp_backoff as utils_backoff

    assert ws_mod.exp_backoff is utils_backoff
    assert watch_mod.exp_backoff is utils_backoff


async def test_send_before_connect_raises():
    """Calling `send_message` before `connect()` is a programmer error."""
    client = TimberbotWsClient("127.0.0.1", 1, auth_token=None)
    with pytest.raises(RuntimeError):
        await client.send_message("ping", {})
    await client.close()


async def test_export_paths_stable():
    """The package exposes both clients at the documented import paths."""
    from timberbot.api import TimberbotClient
    from timberbot.api import TimberbotWsClient as ReexportedWs
    from timberbot.api.models import StatePush as ReexportedStatePush
    from timberbot.api.models import WsMessage as ReexportedWsMessage

    assert TimberbotClient is not None
    assert ReexportedWs is TimberbotWsClient
    assert ReexportedStatePush is StatePush
    assert ReexportedWsMessage is WsMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _next_msg(client: TimberbotWsClient) -> WsMessage:
    """Convenience: pull exactly one message out of the async iterator."""
    agen = client.messages()
    try:
        return await agen.__anext__()
    finally:
        await agen.aclose()


async def _next_msg_keep_open(client: TimberbotWsClient) -> WsMessage:
    """Pull one message but keep the iterator alive on the client.

    Used by tests that need to drive the same `messages()` generator across
    several frames (e.g. reconnect tests).
    """
    if not hasattr(client, "_test_agen"):
        client._test_agen = client.messages()  # type: ignore[attr-defined]
    return await client._test_agen.__anext__()  # type: ignore[attr-defined]
