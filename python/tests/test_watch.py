"""Unit tests for `tbot watch` — the long-running connector.

The endpoints `/api/tbot/heartbeat`, `/api/tbot/register`, and `/api/ping` are
all stubbed with `pytest-httpserver` so the suite is self-contained: it passes
today, and once Unit 1 (#13) lands and these endpoints actually exist on the
mod, the same `WatchLoop` driver runs against the real server.

Each test instantiates a `WatchLoop` with a fake `sleep`/`time_source` so we
can step the loop deterministically without real timers.
"""
from __future__ import annotations

import queue
import threading

import pytest

pytest.importorskip("pytest_httpserver")

from timberbot.api.client import TimberbotClient  # noqa: E402
from timberbot.cli.commands import watch as watch_mod  # noqa: E402
from timberbot.cli.commands.watch import (  # noqa: E402
    Trigger,
    WatchConfig,
    WatchLoop,
    exp_backoff,
    start_webhook_listener,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop(
    httpserver,
    cfg: WatchConfig | None = None,
    *,
    dispatched: list[str] | None = None,
    sleeps: list[float] | None = None,
    now: list[float] | None = None,
    trigger_queue: queue.Queue[Trigger] | None = None,
) -> tuple[WatchLoop, list[str], list[float]]:
    """Construct a `WatchLoop` wired up against `httpserver` with fakes."""
    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)
    dispatched = dispatched if dispatched is not None else []
    sleeps = sleeps if sleeps is not None else []

    def dispatch_fn(goal: str) -> int:
        dispatched.append(goal)
        return 0

    def sleep(dt: float) -> None:
        sleeps.append(dt)

    if now is None:
        now = [0.0]

    def time_source() -> float:
        return now[0]

    loop = WatchLoop(
        client,
        cfg or WatchConfig(heartbeat_interval=2.0, autonomous_interval=60.0),
        dispatch_fn=dispatch_fn,
        trigger_queue=trigger_queue,
        sleep=sleep,
        time_source=time_source,
    )
    return loop, dispatched, sleeps


# ---------------------------------------------------------------------------
# Backoff math
# ---------------------------------------------------------------------------


def test_exp_backoff_sequence_caps_at_30s():
    seq = [exp_backoff(i) for i in range(8)]
    # 1, 2, 4, 8, 16, 30 (cap), 30, 30
    assert seq == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


# ---------------------------------------------------------------------------
# Reconnect loop
# ---------------------------------------------------------------------------


def test_reconnects_with_backoff_then_heartbeats(httpserver):
    # First two pings: not ready. Third: ready.
    httpserver.expect_ordered_request("/api/ping").respond_with_json({"ready": False})
    httpserver.expect_ordered_request("/api/ping").respond_with_json({"ready": False})
    httpserver.expect_ordered_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "request", "ready": True,
    })

    loop, dispatched, sleeps = _make_loop(httpserver)

    assert loop.step() is True   # ping #1 -> not ready -> backoff sleep
    assert loop.step() is True   # ping #2 -> not ready -> backoff sleep
    assert loop.step() is True   # ping #3 -> ready -> connected
    assert loop.connected is True
    assert loop.reconnect_attempts == 0
    # Backoff sleeps: 1s after attempt 0, 2s after attempt 1.
    assert sleeps[:2] == [1.0, 2.0]
    assert len(sleeps) == 2  # no sleep on a successful connect step

    # Heartbeat tick — no pending, no autonomous: paces by the heartbeat interval.
    assert loop.step() is True
    assert dispatched == []
    assert sleeps[-1] == 2.0


def test_disconnect_on_heartbeat_failure_resets_to_reconnect(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    # Heartbeat fails with a server error.
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_data(
        "boom", status=500,
    )

    loop, _, _ = _make_loop(httpserver)
    assert loop.step() is True
    assert loop.connected is True
    # Heartbeat raises → connected flips back off.
    assert loop.step() is True
    assert loop.connected is False


# ---------------------------------------------------------------------------
# Pending request (slow path)
# ---------------------------------------------------------------------------


def test_pending_request_dispatched_and_acked(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})

    # First heartbeat returns a pending request; subsequent heartbeats are
    # empty. Use a stateful handler so the order is well-defined regardless
    # of how pytest-httpserver interleaves ordered vs. regular handlers.
    state = {"count": 0}

    def heartbeat_handler(request):
        from werkzeug.wrappers import Response
        state["count"] += 1
        if state["count"] == 1:
            payload = {
                "mode": "request", "ready": True,
                "pendingRequest": {"id": "req-42", "goal": "plant carrots"},
            }
        else:
            payload = {"mode": "request", "ready": True}
        import json as _json
        return Response(_json.dumps(payload), mimetype="application/json")

    httpserver.expect_request("/api/tbot/heartbeat").respond_with_handler(heartbeat_handler)

    loop, dispatched, _ = _make_loop(httpserver, WatchConfig(once=False))
    assert loop.step() is True   # connect
    assert loop.step() is True   # heartbeat → pending → dispatch
    assert dispatched == ["plant carrots"]
    assert loop.acked_request_id == "req-42"
    assert loop.triggers_fired == 1
    assert loop.step() is True   # next heartbeat carries the ack

    requests = [r for r, _ in httpserver.log if r.path == "/api/tbot/heartbeat"]
    assert len(requests) == 2
    first_body = requests[0].get_json()
    assert first_body["acked_request_id"] is None
    second_body = requests[1].get_json()
    assert second_body["acked_request_id"] == "req-42"


def test_once_flag_exits_after_first_trigger(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "request",
        "ready": True,
        "pendingRequest": {"id": "req-1", "goal": "do it"},
    })

    loop, dispatched, _ = _make_loop(httpserver, WatchConfig(once=True))
    assert loop.step() is True   # connect
    assert loop.step() is False  # heartbeat → dispatch → exit
    assert dispatched == ["do it"]


# ---------------------------------------------------------------------------
# Webhook (fast path)
# ---------------------------------------------------------------------------


def test_webhook_queue_takes_priority_over_pending(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "request",
        "ready": True,
        "pendingRequest": {"id": "slow-1", "goal": "slow path goal"},
    })

    tq: queue.Queue[Trigger] = queue.Queue()
    tq.put(Trigger(source="webhook", goal="fast path goal", request_id="fast-1"))

    loop, dispatched, _ = _make_loop(httpserver, WatchConfig(once=True), trigger_queue=tq)
    assert loop.step() is True   # connect
    assert loop.step() is False  # webhook beats pending; once=True → exit
    assert dispatched == ["fast path goal"]
    assert loop.acked_request_id == "fast-1"


# ---------------------------------------------------------------------------
# Autonomous cadence
# ---------------------------------------------------------------------------


def test_autonomous_fires_only_after_interval_elapses(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "autonomous",
        "ready": True,
        "goal": "stockpile food",
    })

    now = [0.0]
    sleeps: list[float] = []
    dispatched: list[str] = []
    loop, _, _ = _make_loop(
        httpserver,
        WatchConfig(heartbeat_interval=2.0, autonomous_interval=10.0),
        dispatched=dispatched,
        sleeps=sleeps,
        now=now,
    )

    # Step 1: connect. Step 2: first heartbeat → autonomous fires.
    assert loop.step() is True
    assert loop.step() is True
    assert dispatched == ["stockpile food"]

    # Advance time by less than the interval — should NOT fire again.
    now[0] = 5.0
    assert loop.step() is True
    assert dispatched == ["stockpile food"]

    # Advance past the interval — fires again.
    now[0] = 11.0
    assert loop.step() is True
    assert dispatched == ["stockpile food", "stockpile food"]


def test_autonomous_does_not_fire_when_gate_closed(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "autonomous",
        "ready": False,         # gate closed
        "goal": "anything",
    })

    loop, dispatched, _ = _make_loop(
        httpserver, WatchConfig(autonomous_interval=0.0),
    )
    assert loop.step() is True
    assert loop.step() is True
    assert dispatched == []


# ---------------------------------------------------------------------------
# Register flow
# ---------------------------------------------------------------------------


def test_register_called_once_on_connect_when_webhook_url_set(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/register").respond_with_json({"ok": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "request", "ready": True,
    })

    cfg = WatchConfig(webhook_url="http://127.0.0.1:9999/trigger")
    loop, _, _ = _make_loop(httpserver, cfg)

    assert loop.step() is True   # connect
    assert loop.step() is True   # register
    assert loop.registered is True
    assert loop.step() is True   # heartbeat — register NOT called again
    assert loop.step() is True   # heartbeat — register NOT called again

    register_calls = [r for r, _ in httpserver.log if r.path == "/api/tbot/register"]
    assert len(register_calls) == 1
    body = register_calls[0].get_json()
    assert body["webhook_url"] == "http://127.0.0.1:9999/trigger"


def test_no_register_when_webhook_url_unset(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "request", "ready": True,
    })

    loop, _, _ = _make_loop(httpserver, WatchConfig(webhook_url=None))
    assert loop.step() is True   # connect
    assert loop.step() is True   # heartbeat (skips register)
    register_calls = [r for r, _ in httpserver.log if r.path == "/api/tbot/register"]
    assert register_calls == []


# ---------------------------------------------------------------------------
# Webhook listener (real HTTP)
# ---------------------------------------------------------------------------


def test_webhook_listener_enqueues_trigger():
    import json
    import urllib.request

    tq: queue.Queue[Trigger] = queue.Queue()
    server, url = start_webhook_listener(0, tq)  # port=0 → pick a free one
    try:
        body = json.dumps({"goal": "tidy queues", "requestId": "wh-1"}).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 202

        trigger = tq.get(timeout=2)
        assert trigger.source == "webhook"
        assert trigger.goal == "tidy queues"
        assert trigger.request_id == "wh-1"
    finally:
        server.shutdown()
        server.server_close()


def test_webhook_listener_rejects_missing_goal():
    import json
    import urllib.error
    import urllib.request

    tq: queue.Queue[Trigger] = queue.Queue()
    server, url = start_webhook_listener(0, tq)
    try:
        body = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=2)
        assert excinfo.value.code == 400
        assert tq.empty()
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_watch_command_registered_in_main():
    from timberbot.cli.main import _build_registry

    registry = _build_registry()
    cmd = registry.get("watch")
    assert cmd is not None
    assert cmd.handler is watch_mod.run


def test_stop_event_exits_loop(httpserver):
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/tbot/heartbeat").respond_with_json({
        "mode": "request", "ready": True,
    })

    stop = threading.Event()
    loop, _, _ = _make_loop(httpserver)
    loop._stop = stop
    stop.set()
    assert loop.step() is False
