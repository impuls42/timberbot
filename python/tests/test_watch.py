"""Unit tests for `tbot watch` — the WebSocket-only connector.

The WS server (Unit 1 / #28) and Python WS client wrapper (Unit 2 / #29) are
sibling PRs and may not be present yet on this branch. Tests stub the WS
client with `FakeWsClient`, which honours the same `WsClientProtocol` the
production `TimberbotWsClient` will satisfy. Once #29 merges and the typed
client is importable, swapping `FakeWsClient` for the real type is a
one-line constructor change.

Async tests don't depend on `pytest-asyncio` — they wrap the body in
`asyncio.run(...)` so the suite runs on a stock pytest install.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from timberbot.__about__ import __version__
from timberbot.api.client import TimberbotClient
from timberbot.cli.commands import watch as watch_mod
from timberbot.cli.commands.watch import (
    Trigger,
    WatchConfig,
    WatchLoop,
    WsMessage,
    exp_backoff,
    resolve_ws_port,
)

# ---------------------------------------------------------------------------
# Fake WS client
# ---------------------------------------------------------------------------


class FakeWsClient:
    """In-memory WS client double.

    The test feeds messages with `push(type, payload)`; `messages()` iterates
    them in order, blocking on an internal queue. `close()` ends the
    iteration. Outbound `send_message` calls land in `sent` so cadence and
    payloads can be asserted.
    """

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.connect_calls = 0
        self.sent: list[tuple[str, dict]] = []
        self._inbox: asyncio.Queue[WsMessage | None] = asyncio.Queue()

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    async def send_message(self, type: str, payload: dict) -> None:  # noqa: A002
        self.sent.append((type, payload))

    async def messages(self) -> AsyncIterator[WsMessage]:
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.closed = True
        # Unblock any pending `messages()` consumer.
        await self._inbox.put(None)

    # Test-only helpers.

    def push(self, type: str, payload: dict) -> None:  # noqa: A002
        self._inbox.put_nowait(WsMessage(type=type, payload=payload))

    def end_stream(self) -> None:
        """Signal end-of-stream so the message pump can exit cleanly."""
        self._inbox.put_nowait(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop(
    cfg: WatchConfig | None = None,
    *,
    dispatched: list[str] | None = None,
    now: list[float] | None = None,
    ws: FakeWsClient | None = None,
) -> tuple[WatchLoop, FakeWsClient, list[str]]:
    """Build a WatchLoop wired with a `FakeWsClient` and deterministic time."""
    ws = ws or FakeWsClient()
    dispatched = dispatched if dispatched is not None else []
    client = TimberbotClient(host="127.0.0.1", port=8085, json_mode=True)

    def dispatch_fn(goal: str) -> int:
        dispatched.append(goal)
        return 0

    if now is None:
        now = [0.0]

    def time_source() -> float:
        return now[0]

    loop = WatchLoop(
        client,
        cfg or WatchConfig(heartbeat_interval=30.0, autonomous_interval=60.0),
        ws,
        dispatch_fn=dispatch_fn,
        time_source=time_source,
    )
    return loop, ws, dispatched


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


def test_exp_backoff_sequence_caps_at_30s():
    seq = [exp_backoff(i) for i in range(8)]
    # 1, 2, 4, 8, 16, 30 (cap), 30, 30
    assert seq == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


# ---------------------------------------------------------------------------
# WS port resolution
# ---------------------------------------------------------------------------


def test_resolve_ws_port_explicit_wins(monkeypatch):
    monkeypatch.setenv("TBOT_WS_PORT", "9999")
    assert resolve_ws_port(1234, user_config={"ws_port": 5555}) == 1234


def test_resolve_ws_port_env_var(monkeypatch):
    monkeypatch.setenv("TBOT_WS_PORT", "9999")
    assert resolve_ws_port(user_config={}) == 9999


def test_resolve_ws_port_user_config(monkeypatch):
    monkeypatch.delenv("TBOT_WS_PORT", raising=False)
    assert resolve_ws_port(user_config={"ws_port": 4242}) == 4242


def test_resolve_ws_port_default(monkeypatch):
    monkeypatch.delenv("TBOT_WS_PORT", raising=False)
    assert resolve_ws_port(user_config={}) == 8086


def test_resolve_ws_port_ignores_bad_env(monkeypatch):
    monkeypatch.setenv("TBOT_WS_PORT", "not-a-number")
    with pytest.warns(UserWarning, match="TBOT_WS_PORT"):
        assert resolve_ws_port(user_config={}) == 8086


# ---------------------------------------------------------------------------
# Trigger picking (sync, no asyncio)
# ---------------------------------------------------------------------------


def test_pick_trigger_pending_request():
    loop, _, _ = _make_loop()
    trigger = loop.pick_trigger({
        "mode": "request", "ready": True,
        "pendingRequest": {"id": "req-1", "goal": "plant carrots"},
    })
    assert trigger == Trigger(source="pending", goal="plant carrots", request_id="req-1")


def test_pick_trigger_de_dupes_same_pending_id():
    loop, _, _ = _make_loop()
    state = {
        "mode": "request", "ready": True,
        "pendingRequest": {"id": "req-1", "goal": "plant carrots"},
    }
    assert loop.pick_trigger(state) is not None
    # Same pending push — should NOT fire again.
    assert loop.pick_trigger(state) is None


def test_pick_trigger_autonomous_respects_cadence():
    now = [0.0]
    loop, _, _ = _make_loop(
        WatchConfig(heartbeat_interval=30.0, autonomous_interval=10.0),
        now=now,
    )
    state = {"mode": "autonomous", "ready": True, "goal": "stockpile food"}
    assert loop.pick_trigger(state) == Trigger(
        source="autonomous", goal="stockpile food", request_id=None,
    )
    # Within the interval — no fire.
    now[0] = 5.0
    assert loop.pick_trigger(state) is None
    # Past the interval — fires again.
    now[0] = 11.0
    assert loop.pick_trigger(state) is not None


def test_pick_trigger_autonomous_gate_closed():
    loop, _, _ = _make_loop(
        WatchConfig(heartbeat_interval=30.0, autonomous_interval=0.0),
    )
    state = {"mode": "autonomous", "ready": False, "goal": "anything"}
    assert loop.pick_trigger(state) is None


def test_pick_trigger_no_pending_no_autonomous():
    loop, _, _ = _make_loop()
    assert loop.pick_trigger({"mode": "request", "ready": True}) is None


def test_pick_trigger_handles_garbage_state():
    loop, _, _ = _make_loop()
    assert loop.pick_trigger("not a dict") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Heartbeat payload + ack flow
# ---------------------------------------------------------------------------


def test_heartbeat_payload_includes_acked_request_id():
    loop, _, _ = _make_loop()
    assert loop.build_heartbeat_payload() == {
        "version": __version__,
        "agent_status": "idle",
        "acked_request_id": None,
    }
    loop.acked_request_id = "req-42"
    loop.agent_status = "busy"
    payload = loop.build_heartbeat_payload()
    assert payload["acked_request_id"] == "req-42"
    assert payload["agent_status"] == "busy"


def test_note_dispatch_advances_ack_and_counter():
    loop, _, _ = _make_loop()
    loop.note_dispatch(Trigger(source="pending", goal="g", request_id="req-9"))
    assert loop.acked_request_id == "req-9"
    assert loop.triggers_fired == 1


def test_note_dispatch_autonomous_does_not_set_ack():
    loop, _, _ = _make_loop()
    loop.note_dispatch(Trigger(source="autonomous", goal="g"))
    assert loop.acked_request_id is None
    assert loop.triggers_fired == 1


def test_note_dispatch_once_marks_exit():
    loop, _, _ = _make_loop(WatchConfig(once=True))
    loop.note_dispatch(Trigger(source="pending", goal="g", request_id="r"))
    assert loop._should_exit is True


# ---------------------------------------------------------------------------
# Async integration: message pump + dispatch
# ---------------------------------------------------------------------------


def test_state_push_dispatches_and_acks_via_heartbeat():
    async def scenario() -> None:
        loop, ws, dispatched = _make_loop(WatchConfig(
            heartbeat_interval=0.05, autonomous_interval=60.0,
        ))
        ws.push("state", {
            "mode": "request", "ready": True,
            "pendingRequest": {"id": "req-7", "goal": "build a sawmill"},
        })
        # Schedule end-of-stream after the pump processes the state frame.
        async def _ender() -> None:
            await asyncio.sleep(0.2)
            ws.end_stream()
            loop.stop()
        ender = asyncio.create_task(_ender())
        rc = await loop.run()
        await ender
        assert rc == 0
        assert dispatched == ["build a sawmill"]
        assert loop.acked_request_id == "req-7"
        # At least one heartbeat fired with the post-ack id.
        hb_frames = [p for t, p in ws.sent if t == "heartbeat"]
        assert hb_frames, "no heartbeat frames were sent"
        # If we sent more than one, the last one must carry the ack — earlier
        # ticks may have raced ahead of the dispatch.
        assert hb_frames[-1]["acked_request_id"] == "req-7"

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_autonomous_state_push_dispatches():
    async def scenario() -> None:
        now = [0.0]
        loop, ws, dispatched = _make_loop(
            WatchConfig(heartbeat_interval=10.0, autonomous_interval=10.0, once=True),
            now=now,
        )
        ws.push("state", {
            "mode": "autonomous", "ready": True, "goal": "stockpile food",
        })
        # Pump exits on its own thanks to once=True; backstop with a deadline.
        async def _ender() -> None:
            await asyncio.sleep(1.0)
            ws.end_stream()
            loop.stop()
        ender = asyncio.create_task(_ender())
        rc = await loop.run()
        await ender
        assert rc == 0
        assert dispatched == ["stockpile food"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_dispatch_crash_does_not_kill_pump():
    """A throwing dispatch_fn is logged but the pump keeps going."""

    def boom(_goal: str) -> int:
        raise RuntimeError("dispatch boom")

    async def scenario() -> None:
        ws = FakeWsClient()
        cfg = WatchConfig(heartbeat_interval=10.0, autonomous_interval=60.0)
        client = TimberbotClient(host="127.0.0.1", port=8085, json_mode=True)
        loop = WatchLoop(client, cfg, ws, dispatch_fn=boom)

        ws.push("state", {
            "mode": "request", "ready": True,
            "pendingRequest": {"id": "req-x", "goal": "explode"},
        })

        async def _ender() -> None:
            await asyncio.sleep(0.2)
            ws.end_stream()
            loop.stop()

        ender = asyncio.create_task(_ender())
        rc = await loop.run()
        await ender
        assert rc == 0
        # Even though dispatch threw, the ack still advances — failures are a
        # "we tried" signal so the mod can clear the slot.
        assert loop.acked_request_id == "req-x"
        assert loop.triggers_fired == 1

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_heartbeat_cadence_sends_at_interval():
    async def scenario() -> None:
        loop, ws, _ = _make_loop(WatchConfig(
            heartbeat_interval=0.05, autonomous_interval=60.0,
        ))

        async def _ender() -> None:
            # Allow ~3 heartbeat ticks to fire (0.18s > 3 * 0.05s).
            await asyncio.sleep(0.18)
            ws.end_stream()
            loop.stop()

        ender = asyncio.create_task(_ender())
        rc = await loop.run()
        await ender
        assert rc == 0
        hb_frames = [p for t, p in ws.sent if t == "heartbeat"]
        assert len(hb_frames) >= 2, f"expected >=2 heartbeats, got {len(hb_frames)}"
        for p in hb_frames:
            assert set(p.keys()) == {"version", "agent_status", "acked_request_id"}

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_message_pump_exits_on_ws_close():
    """When the WS client signals end-of-stream, the pump returns cleanly."""

    async def scenario() -> None:
        loop, ws, _ = _make_loop(WatchConfig(
            heartbeat_interval=10.0, autonomous_interval=60.0,
        ))
        # End the stream immediately — pump should observe close and stop.
        ws.end_stream()
        rc = await loop.run()
        assert rc == 0
        assert ws.closed is True

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_initial_connect_failure_returns_nonzero():
    class FailingWs(FakeWsClient):
        async def connect(self) -> None:
            raise RuntimeError("connect refused")

    async def scenario() -> None:
        ws = FailingWs()
        cfg = WatchConfig(heartbeat_interval=10.0, autonomous_interval=60.0)
        client = TimberbotClient(host="127.0.0.1", port=8085, json_mode=True)
        loop = WatchLoop(client, cfg, ws)
        rc = await loop.run()
        assert rc == 1

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_non_state_frames_are_handled_without_dispatch():
    """`event`, `error`, `pong`, and unknown frame types must not trigger dispatch."""

    async def scenario() -> None:
        loop, ws, dispatched = _make_loop(WatchConfig(
            heartbeat_interval=10.0, autonomous_interval=60.0,
        ))
        ws.push("event", {"event": "drought.start", "day": 45})
        ws.push("error", {"error": "transient"})
        ws.push("pong", {})
        ws.push("mystery", {"foo": "bar"})

        async def _ender() -> None:
            await asyncio.sleep(0.15)
            ws.end_stream()
            loop.stop()

        ender = asyncio.create_task(_ender())
        rc = await loop.run()
        await ender
        assert rc == 0
        assert dispatched == []

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_once_flag_exits_after_first_dispatch():
    async def scenario() -> None:
        loop, ws, dispatched = _make_loop(WatchConfig(
            heartbeat_interval=10.0, autonomous_interval=60.0, once=True,
        ))
        ws.push("state", {
            "mode": "request", "ready": True,
            "pendingRequest": {"id": "req-once", "goal": "do it"},
        })
        # Push another state that *would* fire if we were still alive.
        ws.push("state", {
            "mode": "request", "ready": True,
            "pendingRequest": {"id": "req-second", "goal": "should not fire"},
        })

        async def _ender() -> None:
            await asyncio.sleep(1.0)  # Backstop; once should trip first.
            ws.end_stream()
            loop.stop()

        ender = asyncio.create_task(_ender())
        rc = await loop.run()
        ender.cancel()
        assert rc == 0
        assert dispatched == ["do it"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_stop_method_unblocks_run():
    """`loop.stop()` from another task ends `run()` even mid-pump."""

    async def scenario() -> None:
        loop, ws, _ = _make_loop(WatchConfig(
            heartbeat_interval=10.0, autonomous_interval=60.0,
        ))

        async def _stopper() -> None:
            await asyncio.sleep(0.05)
            loop.stop()
            ws.end_stream()

        stopper = asyncio.create_task(_stopper())
        rc = await loop.run()
        await stopper
        assert rc == 0

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_watch_command_registered_on_tbot_class(monkeypatch):
    # `timberbot.cli.__init__` re-exports `main`, shadowing the submodule
    # attribute. Pull the actual module from sys.modules instead.
    import importlib
    cli_main = importlib.import_module("timberbot.cli.main")

    assert hasattr(cli_main.Tbot, "watch")

    # `Tbot.watch` is now an instance method that threads the global flags
    # from `_CTX` into `watch_mod.watch`. Verify the indirection by patching
    # the underlying function and checking it gets called.
    captured: dict[str, object] = {}

    def fake_watch(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_main, "watch", fake_watch)
    monkeypatch.setattr(cli_main, "_CTX", cli_main.GlobalFlags(host="10.0.0.5", port=9001))
    cli_main.Tbot().watch(backend="claude", once=True)
    assert captured["backend"] == "claude"
    assert captured["once"] is True
    assert captured["host"] == "10.0.0.5"
    assert captured["port"] == 9001


def test_watch_threads_global_port_into_client(monkeypatch):
    """Regression for Bug A: `watch()` was dropping the port from
    `resolve_endpoint(host, port)` and constructing `TimberbotClient(host=…,
    auth_token=…, json_mode=True)` without `port`. The HTTP client used by
    the connector would silently target port 8085 even when the user passed
    `tbot --port=9090 watch`."""
    constructed: dict[str, object] = {}

    class _StubClient:
        def __init__(self, **kwargs):
            constructed.update(kwargs)

    class _StubLoop:
        def __init__(self, *_a, **_kw):
            pass

        async def run(self):
            return 0

    monkeypatch.setattr(watch_mod, "TimberbotClient", _StubClient)
    monkeypatch.setattr(watch_mod, "WatchLoop", _StubLoop)
    monkeypatch.setattr(
        watch_mod, "resolve_endpoint",
        lambda h, p: (h or "127.0.0.1", p or 8085),
    )
    monkeypatch.setattr(watch_mod, "resolve_ws_port", lambda *_a, **_kw: 8086)
    monkeypatch.setattr(watch_mod, "resolve_auth_token", lambda t=None: t)
    monkeypatch.setattr(
        watch_mod, "_default_ws_client",
        lambda *_a, **_kw: object(),
    )

    rc = watch_mod.watch(host="10.0.0.5", port=9090, auth_token="tok")
    assert rc == 0
    assert constructed["host"] == "10.0.0.5"
    assert constructed["port"] == 9090, "watch() must forward port to TimberbotClient"
    assert constructed["auth_token"] == "tok"


def test_fire_signature_accepts_ws_port_flag():
    """Fire reflects `watch(...)`'s signature into CLI flags."""
    import inspect

    params = inspect.signature(watch_mod.watch).parameters
    assert "ws_port" in params
    assert "once" in params


def test_fire_signature_has_no_legacy_listen_port():
    """The legacy `--listen-port` flag is gone — confirm it's no longer a parameter."""
    import inspect

    params = inspect.signature(watch_mod.watch).parameters
    assert "listen_port" not in params


def test_no_webhook_listener_module_state():
    """Confirm the embedded HTTP listener pieces are no longer exported."""
    assert not hasattr(watch_mod, "_WebhookHandler")
    assert not hasattr(watch_mod, "start_webhook_listener")


def test_no_more_http_polling_helpers():
    """Step-driven polling helpers are gone post-rework."""
    # The WatchLoop class no longer has these methods.
    assert not hasattr(WatchLoop, "_step_connect")
    assert not hasattr(WatchLoop, "_step_register")
    assert not hasattr(WatchLoop, "_step_heartbeat")
    assert not hasattr(WatchLoop, "step")
