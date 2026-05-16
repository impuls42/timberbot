"""`tbot watch` — long-running connector that polls the mod and dispatches `agent run`.

Architecture:

```
┌────────────────────────────────────────────────────────────────────────┐
│  tbot watch (this process)                                             │
│                                                                        │
│    ┌──────────────┐    ping with exp backoff (1s→30s cap)              │
│    │ reconnect    │───────────────────────────────────────────►  /api/ping
│    └──────┬───────┘                                                    │
│           │ connected                                                  │
│           ▼                                                            │
│    ┌──────────────┐    POST {webhook_url} (optional)                   │
│    │ register     │───────────────────────────────────────────►  /api/tbot/register
│    └──────┬───────┘                                                    │
│           ▼                                                            │
│    ┌──────────────┐    every 2s; carries acked_request_id              │
│    │ heartbeat    │◄──────────────────────────────────────────►  /api/tbot/heartbeat
│    └──────┬───────┘    response: {ready, mode, pendingRequest, ...}    │
│           │                                                            │
│           ▼                                                            │
│    ┌──────────────┐    triggers: pending request | webhook | autonomous│
│    │ dispatch     │   ─►  run_agent(goal=...)  ─►  advance ack         │
│    └──────────────┘                                                    │
└────────────────────────────────────────────────────────────────────────┘
```

The control loop is `WatchLoop.run_once_iteration`, which:

  - on disconnect: backs off and reconnects;
  - on connect: registers webhook URL (if a local listener was configured);
  - sends a heartbeat;
  - applies dispatch policy on the response;
  - sleeps until the next heartbeat tick.

All time-related calls go through `time_source` and `sleep` callables so tests
can drive the loop deterministically without real sleeps.

The endpoints `/api/tbot/heartbeat`, `/api/tbot/register`, `/api/agent/state`,
`/api/agent/request`, and `/api/ready` are defined by Unit 1 (#13). Until that
unit lands, calls will fail at runtime — but the unit tests for this command
stub them with `pytest-httpserver`, so the PR is self-verifying.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import queue
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from timberbot.__about__ import __version__
from timberbot.agent.runner import run_agent
from timberbot.api.client import TimberbotClient
from timberbot.config import config_dir
from timberbot.utils import exp_backoff

log = logging.getLogger("timberbot.watch")

# Re-exported so `from timberbot.cli.commands.watch import exp_backoff` keeps
# working — the function actually lives in `timberbot.utils` (shared with the
# WS client) to avoid `api/` importing from `cli/commands/`.
__all__ = ["exp_backoff"]


# ---------------------------------------------------------------------------
# Trigger queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """Source-tagged request to dispatch an agent cycle."""

    source: str  # "webhook" | "pending" | "autonomous"
    goal: str
    request_id: str | None = None


# ---------------------------------------------------------------------------
# Local webhook listener
# ---------------------------------------------------------------------------


class _WebhookHandler(BaseHTTPRequestHandler):
    """Minimal POST receiver that forwards `{goal, requestId}` to a `Queue`."""

    # Class-level placeholder. `start_webhook_listener` builds a subclass via
    # `type(..., {"trigger_queue": tq})` so each listener instance binds its
    # own queue. Never read this directly without going through that factory.
    trigger_queue: queue.Queue[Trigger] | None = None

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the default stderr access log; we log via `log` instead.
        log.debug("webhook %s %s", self.address_string(), format % args)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler protocol)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        goal = str(payload.get("goal") or payload.get("prompt") or "").strip()
        if not goal:
            self.send_response(400)
            self.end_headers()
            return
        request_id = payload.get("requestId") or payload.get("id")
        if self.trigger_queue is not None:
            self.trigger_queue.put(Trigger(
                source="webhook",
                goal=goal,
                request_id=str(request_id) if request_id is not None else None,
            ))
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_webhook_listener(
    port: int,
    trigger_queue: queue.Queue[Trigger],
    *,
    host: str = "127.0.0.1",
) -> tuple[ThreadingHTTPServer, str]:
    """Start a background HTTP server. Returns `(server, webhook_url)`.

    The server is bound to localhost only; the connector is meant to run on
    the same host as the game. Caller is responsible for `server.shutdown()`.
    """
    handler_cls = type("_BoundWebhookHandler", (_WebhookHandler,), {
        "trigger_queue": trigger_queue,
    })
    server = ThreadingHTTPServer((host, port), handler_cls)
    # If port=0 the OS picked one; reflect it in the URL.
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    if bound_host in ("0.0.0.0", "::"):
        bound_host = socket.gethostname()
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="timberbot-watch-webhook")
    thread.start()
    webhook_url = f"http://{bound_host}:{bound_port}/trigger"
    log.info("watch: webhook listener bound on %s", webhook_url)
    return server, webhook_url


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------


@dataclass
class WatchConfig:
    """Tunables for `WatchLoop`. Defaults match the `tbot watch` CLI defaults."""

    backend: str = "claude"
    model: str | None = None
    effort: str | None = None
    prompt_name: str = "timberbot"
    extra_prompt_names: list[str] = field(default_factory=lambda: ["connector-mode"])
    heartbeat_interval: float = 2.0
    autonomous_interval: float = 60.0
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    once: bool = False
    webhook_url: str | None = None


# Function-call abstraction used by the loop. Tests override with a stub.
DispatchFn = Callable[[str], int]


def default_dispatch_factory(
    cfg: WatchConfig,
    client: TimberbotClient,
) -> DispatchFn:
    """Build the per-cycle dispatcher: a no-arg callable accepting just `goal`."""

    def _dispatch(goal: str) -> int:
        return run_agent(
            backend=cfg.backend,
            goal=goal,
            model=cfg.model,
            effort=cfg.effort,
            prompt_name=cfg.prompt_name,
            extra_prompt_names=cfg.extra_prompt_names,
            client=client,
            user_config_dir=config_dir(),
            check_connection=False,  # watch owns the connection state.
        )

    return _dispatch


class WatchLoop:
    """The connector's main control loop.

    Lifecycle (one full pass = `step()`):

    1. If not connected, attempt `client.ping()` with exponential backoff.
    2. On first successful connect, optionally POST `/api/tbot/register`.
    3. Send `POST /api/tbot/heartbeat`, parse response.
    4. Apply trigger policy:
       (a) drain `trigger_queue` (webhook fast path) — highest priority;
       (b) `state.pendingRequest` from the heartbeat;
       (c) autonomous cadence when `mode == "autonomous"` and `ready` is True.
    5. Sleep `heartbeat_interval` (driven by `sleep` callable).

    Returns False from `step()` when the loop should stop (e.g. `--once`
    and one trigger has fired).
    """

    def __init__(
        self,
        client: TimberbotClient,
        cfg: WatchConfig,
        *,
        dispatch_fn: DispatchFn | None = None,
        trigger_queue: queue.Queue[Trigger] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        time_source: Callable[[], float] = time.monotonic,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.dispatch_fn = dispatch_fn or default_dispatch_factory(cfg, client)
        self.trigger_queue = trigger_queue or queue.Queue()
        self._sleep = sleep
        self._now = time_source
        self._stop = stop_event or threading.Event()

        # State carried across iterations:
        self.connected = False
        self.registered = False
        self.reconnect_attempts = 0
        self.last_autonomous_run: float = -1.0  # time of last autonomous fire
        self.acked_request_id: str | None = None
        self.last_state: dict[str, Any] | None = None
        self.triggers_fired = 0

    # -- HTTP helpers (kept thin so tests can read the call sequence) --
    #
    # These call `client._post` (the underscored internal) because the
    # `/api/tbot/heartbeat` and `/api/tbot/register` endpoints land in Unit 1
    # (#13) and don't yet have typed wrappers on `TimberbotClient`. Once Unit 1
    # promotes them to public methods (e.g. `client.heartbeat(...)`,
    # `client.register(...)`), this file should switch to those — see #15.

    def _heartbeat(self, agent_status: str = "idle") -> dict[str, Any]:
        body = {
            "version": __version__,
            "agent_status": agent_status,
            "acked_request_id": self.acked_request_id,
        }
        return self.client._post("/api/tbot/heartbeat", body)

    def _register(self) -> dict[str, Any]:
        assert self.cfg.webhook_url is not None
        return self.client._post(
            "/api/tbot/register",
            {"webhook_url": self.cfg.webhook_url},
        )

    # -- Trigger picking --

    def _pick_trigger(self, state: dict[str, Any]) -> Trigger | None:
        # Webhook (fast path) drains first.
        with contextlib.suppress(queue.Empty):
            return self.trigger_queue.get_nowait()

        # Pending request from heartbeat.
        pending = state.get("pendingRequest") if isinstance(state, dict) else None
        if isinstance(pending, dict) and pending.get("goal"):
            return Trigger(
                source="pending",
                goal=str(pending["goal"]),
                request_id=str(pending.get("id")) if pending.get("id") is not None else None,
            )

        # Autonomous cadence.
        mode = state.get("mode") if isinstance(state, dict) else None
        ready = bool(state.get("ready")) if isinstance(state, dict) else False
        if mode == "autonomous" and ready:
            now = self._now()
            if (self.last_autonomous_run < 0
                    or (now - self.last_autonomous_run) >= self.cfg.autonomous_interval):
                self.last_autonomous_run = now
                goal = str(state.get("goal") or "")
                return Trigger(source="autonomous", goal=goal, request_id=None)

        return None

    # -- One step of the loop --
    #
    # Each call advances exactly one phase: reconnect → register → heartbeat.
    # This keeps the control flow trivial to test step-by-step and matches the
    # observable wall-clock cadence (one HTTP round-trip per tick).

    def step(self) -> bool:
        """Run one iteration. Returns True to keep looping, False to exit."""
        if self._stop.is_set():
            return False

        if not self.connected:
            return self._step_connect()

        if not self.registered and self.cfg.webhook_url:
            return self._step_register()

        return self._step_heartbeat()

    def _step_connect(self) -> bool:
        try:
            up = self.client.ping()
        except Exception as exc:  # noqa: BLE001 - any HTTP error is a "down" signal
            log.warning("watch: ping failed (%s); attempt=%d", exc, self.reconnect_attempts)
            up = False
        if not up:
            delay = exp_backoff(self.reconnect_attempts,
                                base=self.cfg.backoff_base, cap=self.cfg.backoff_cap)
            log.info("watch: reconnect attempt %d, sleeping %.1fs",
                     self.reconnect_attempts, delay)
            self.reconnect_attempts += 1
            self._sleep(delay)
            return True
        log.info("watch: connected after %d attempt(s)", self.reconnect_attempts + 1)
        self.connected = True
        self.reconnect_attempts = 0
        self.registered = False  # re-register on every fresh connect
        return True

    def _step_register(self) -> bool:
        try:
            self._register()
        except Exception as exc:  # noqa: BLE001
            log.warning("watch: register failed (%s); will retry next tick", exc)
            # Treat register failure as a transient connection problem.
            self.connected = False
            self.reconnect_attempts = 0
            self._sleep(self.cfg.backoff_base)
            return True
        self.registered = True
        log.info("watch: registered webhook %s", self.cfg.webhook_url)
        return True

    def _step_heartbeat(self) -> bool:
        try:
            state = self._heartbeat()
        except Exception as exc:  # noqa: BLE001
            log.warning("watch: heartbeat failed (%s); marking disconnected", exc)
            self.connected = False
            return True
        self.last_state = state

        trigger = self._pick_trigger(state)
        if trigger is not None:
            log.info("watch: trigger source=%s goal=%r", trigger.source, trigger.goal)
            self.triggers_fired += 1
            try:
                rc = self.dispatch_fn(trigger.goal)
                log.info("watch: cycle done rc=%d", rc)
            except Exception as exc:  # noqa: BLE001
                log.exception("watch: dispatch crashed (%s)", exc)
            if trigger.request_id:
                # Advance the ack so the next heartbeat clears the pending slot.
                self.acked_request_id = trigger.request_id
            if self.cfg.once:
                return False

        self._sleep(self.cfg.heartbeat_interval)
        return True

    def run(self) -> int:
        """Drive `step` until it returns False or `stop()` is called."""
        try:
            while self.step():
                pass
        except KeyboardInterrupt:
            log.info("watch: interrupted")
        return 0

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse(args: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tbot watch", add_help=True)
    p.add_argument("--backend", default="claude",
                   help="Agent backend to dispatch (default: claude).")
    p.add_argument("--model", default=None, help="Model identifier passed to the backend.")
    p.add_argument("--effort", default=None, help="Reasoning effort passed to the backend.")
    p.add_argument("--prompt", dest="prompt_name", default="timberbot",
                   help="Name of the base system prompt to load (default: timberbot).")
    p.add_argument("--listen-port", type=int, default=0,
                   help=("Optional port for a local webhook listener. "
                         "Default 0 disables the listener (no push-mode triggers, "
                         "only heartbeat-driven dispatch)."))
    p.add_argument("--listen-host", default="127.0.0.1",
                   help="Bind host for --listen-port (default: 127.0.0.1).")
    p.add_argument("--autonomous-interval", type=float, default=60.0,
                   help="Seconds between autonomous-mode cycles (default: 60).")
    p.add_argument("--heartbeat-interval", type=float, default=2.0,
                   help="Seconds between heartbeats (default: 2).")
    p.add_argument("--once", action="store_true",
                   help="Run until a single trigger fires, then exit (useful for debugging).")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Increase log verbosity (-v INFO, -vv DEBUG).")
    return p.parse_args(args)


def run(args: list[str]) -> int:
    """Entry point invoked from `tbot watch`."""
    ns = _parse(args)
    _configure_logging(ns.verbose)

    cfg = WatchConfig(
        backend=ns.backend,
        model=ns.model,
        effort=ns.effort,
        prompt_name=ns.prompt_name,
        heartbeat_interval=ns.heartbeat_interval,
        autonomous_interval=ns.autonomous_interval,
        once=ns.once,
    )

    client = TimberbotClient(json_mode=True)
    trigger_queue: queue.Queue[Trigger] = queue.Queue()

    server = None
    if ns.listen_port:
        try:
            server, webhook_url = start_webhook_listener(
                ns.listen_port, trigger_queue, host=ns.listen_host,
            )
        except OSError as exc:
            print(f"error: cannot bind webhook listener on port {ns.listen_port}: {exc}",
                  file=sys.stderr)
            return 1
        cfg.webhook_url = webhook_url

    loop = WatchLoop(client, cfg, trigger_queue=trigger_queue)
    try:
        return loop.run()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def _configure_logging(verbosity: int) -> None:
    """Wire up a minimal stderr logger for `timberbot.*` loggers.

    Applies the verbosity-derived level to the whole `timberbot` package so
    that sub-loggers used by `run_agent` (`timberbot.agent.*`) inherit it too.
    Idempotent — won't double-attach the handler if already configured (e.g.
    by a test harness).
    """
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    pkg = logging.getLogger("timberbot")
    if not pkg.handlers:
        pkg.addHandler(handler)
    pkg.setLevel(level)
