"""`tbot watch` — long-running connector backed by a single WebSocket.

Architecture (post-WS rework — see parent issue #27 and sub-issue #30)::

    ┌────────────────────────────────────────────────────────────────────────┐
    │  tbot watch (this process)                                             │
    │                                                                        │
    │   ┌────────────────┐    WS connect /api/ws on ws_port (default 8086)   │
    │   │ TimberbotWs    │◄──────────────────────────────────────────►  mod  │
    │   │ Client         │    reconnect via exp_backoff(1s→30s)              │
    │   └──────┬─────────┘                                                   │
    │          │ frames {type, payload}                                      │
    │          ▼                                                             │
    │   ┌────────────────┐    state → update view → maybe dispatch           │
    │   │ message pump   │    event → log (consumed by tbot listen too)      │
    │   └──────┬─────────┘                                                   │
    │          │                                                             │
    │   ┌──────┴─────────┐    every 30s; carries acked_request_id            │
    │   │ heartbeat tick │────►  send_message("heartbeat", {...})            │
    │   └──────┬─────────┘                                                   │
    │          │                                                             │
    │   ┌──────┴─────────┐    triggers: pendingRequest | autonomous cadence  │
    │   │ dispatch       │────►  run_agent(goal=...) → advance ack           │
    │   └────────────────┘                                                   │
    └────────────────────────────────────────────────────────────────────────┘

The HTTP polling loop, the `/api/tbot/heartbeat` and `/api/tbot/register`
calls, and the embedded webhook listener are all gone. Events arrive over the
same WebSocket as state pushes, so `tbot watch` no longer needs to host its
own HTTP receiver.

The WS client is `timberbot.api.wsclient.TimberbotWsClient` (lands in
Unit 2 / sub-issue #29). Until that module is importable, `_default_ws_client`
falls back to a thin local wrapper around `aiohttp.ClientSession.ws_connect()`
that implements the same minimal protocol — `connect / send_message /
messages / close`. Once #29 merges to master, the fallback is dead code and
the import can be tightened.

All time-dependent behavior goes through `time_source` / `asyncio.sleep` /
`monotonic()` so tests can drive cadence without real timers. Tests stub the
WS client entirely; no real network is opened.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from timberbot.__about__ import __version__
from timberbot.agent.runner import run_agent
from timberbot.api.client import TimberbotClient
from timberbot.config import config_dir
from timberbot.settings import resolve_auth_token, resolve_endpoint
from timberbot.user_config import client_config
from timberbot.utils import exp_backoff

log = logging.getLogger("timberbot.watch")

# Re-exported so `from timberbot.cli.commands.watch import exp_backoff` keeps
# working — the function actually lives in `timberbot.utils` (shared with the
# WS client) to avoid `api/` importing from `cli/commands/`.
__all__ = ["exp_backoff"]


# ---------------------------------------------------------------------------
# WS port resolution
# ---------------------------------------------------------------------------


def _env_ws_port() -> int | None:
    """Parse `TBOT_WS_PORT` as int, or None if unset/malformed."""
    raw = os.environ.get("TBOT_WS_PORT")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        import warnings
        warnings.warn(
            f"TBOT_WS_PORT='{raw}' is not an integer; ignoring",
            UserWarning, stacklevel=3,
        )
        return None


def resolve_ws_port(
    ws_port: int | None = None,
    user_config: dict[str, Any] | None = None,
) -> int:
    """Resolve the WebSocket port per the same precedence chain as `resolve_endpoint`.

    Order (first wins): explicit arg → `TBOT_WS_PORT` env var → `[client].ws_port`
    in `~/.config/timberbot/config.toml` → built-in default 8086.

    The default matches the mod-side `wsPort` setting decided in #27 (8085
    remains HTTP-only; the WS server listens on a sibling port). `user_config`
    is injectable for tests; production callers leave it None.
    """
    if ws_port is not None:
        return ws_port
    env_port = _env_ws_port()
    if env_port is not None:
        return env_port
    uc = user_config if user_config is not None else client_config()
    cfg_port = uc.get("ws_port")
    if isinstance(cfg_port, int):
        return cfg_port
    return 8086


# ---------------------------------------------------------------------------
# WS client protocol + envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WsMessage:
    """One frame received from the mod.

    The on-wire shape is `{type, payload}`. We accept missing/non-dict
    `payload` defensively so a malformed server frame becomes a logged
    `error` rather than a crashed pump.
    """

    type: str
    payload: dict[str, Any]


class WsClientProtocol(Protocol):
    """Minimum surface the watch loop needs from a WS client.

    `TimberbotWsClient` (#29) satisfies this naturally. Tests pass a
    `FakeWsClient` that drains an `asyncio.Queue`.
    """

    async def connect(self) -> None: ...
    async def send_message(self, type: str, payload: dict[str, Any]) -> None: ...
    def messages(self) -> AsyncIterator[WsMessage]: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Trigger queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """Source-tagged request to dispatch an agent cycle."""

    source: str  # "pending" | "autonomous"
    goal: str
    request_id: str | None = None


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
    heartbeat_interval: float = 30.0
    autonomous_interval: float = 60.0
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    once: bool = False
    host: str = "127.0.0.1"
    ws_port: int = 8086
    auth_token: str | None = None


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
    """The connector's main control loop, driven by a single WebSocket.

    Public sync helpers (testable without an event loop):

      - `pick_trigger(state)`: returns the next `Trigger` derived from the
        in-place state view, accounting for the autonomous cadence clock.
      - `note_dispatch(trigger)`: advances `acked_request_id` and the cycle
        counter after a successful dispatch.
      - `build_heartbeat_payload()`: the body sent every `heartbeat_interval`.

    Async lifecycle:

      - `run()`: connect, then concurrently pump messages and tick heartbeats,
        until `stop()` is called or `cfg.once` fires.
    """

    def __init__(
        self,
        client: TimberbotClient,
        cfg: WatchConfig,
        ws_client: WsClientProtocol,
        *,
        dispatch_fn: DispatchFn | None = None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.ws = ws_client
        self.dispatch_fn = dispatch_fn or default_dispatch_factory(cfg, client)
        self._now = time_source

        # State carried across iterations:
        self.last_autonomous_run: float = -1.0  # monotonic time of last autonomous fire
        self.acked_request_id: str | None = None
        self.last_pending_id: str | None = None  # de-dupe identical pendingRequest pushes
        self.last_state: dict[str, Any] | None = None
        self.triggers_fired = 0
        self.agent_status = "idle"
        # `_stop` is bound in `run()` once we're sure an event loop is running.
        # `stop()` is a no-op before then (e.g. if a test forgets to await run).
        self._stop: asyncio.Event | None = None
        self._should_exit = False  # flips when --once and a cycle ran

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def build_heartbeat_payload(self) -> dict[str, Any]:
        """The body sent in every WS `heartbeat` frame."""
        return {
            "version": __version__,
            "agent_status": self.agent_status,
            "acked_request_id": self.acked_request_id,
        }

    # ------------------------------------------------------------------
    # Trigger selection
    # ------------------------------------------------------------------

    def pick_trigger(self, state: dict[str, Any]) -> Trigger | None:
        """Inspect a freshly-pushed state and decide whether to dispatch.

        Priorities:

          1. `state.pendingRequest` — explicit request mode. De-duped by id so
             a re-push of the same pending slot doesn't re-fire.
          2. Autonomous cadence — `mode == "autonomous"` and `ready == True`,
             gated by `autonomous_interval` elapsed since the last fire.

        Returns None if neither condition fires; that just means the message
        pump goes back to awaiting the next frame.
        """
        if not isinstance(state, dict):
            return None

        pending = state.get("pendingRequest")
        if isinstance(pending, dict) and pending.get("goal"):
            pid = str(pending.get("id")) if pending.get("id") is not None else None
            if pid != self.last_pending_id:
                self.last_pending_id = pid
                return Trigger(
                    source="pending",
                    goal=str(pending["goal"]),
                    request_id=pid,
                )

        mode = state.get("mode")
        ready = bool(state.get("ready"))
        if mode == "autonomous" and ready:
            now = self._now()
            if (self.last_autonomous_run < 0
                    or (now - self.last_autonomous_run) >= self.cfg.autonomous_interval):
                self.last_autonomous_run = now
                goal = str(state.get("goal") or "")
                return Trigger(source="autonomous", goal=goal, request_id=None)

        return None

    def note_dispatch(self, trigger: Trigger) -> None:
        """Bookkeeping after a dispatch returns. Always called even on crash."""
        self.triggers_fired += 1
        if trigger.request_id:
            self.acked_request_id = trigger.request_id
        if self.cfg.once:
            self._should_exit = True

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def _heartbeat_task(self) -> None:
        """Send `heartbeat` frames at `heartbeat_interval`. Stops on `_stop`."""
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.ws.send_message("heartbeat", self.build_heartbeat_payload())
            except Exception as exc:  # noqa: BLE001 - any send error means reconnect time
                log.warning("watch: heartbeat send failed (%s)", exc)
                # The WS client owns reconnect; we just wait and try the next tick.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.heartbeat_interval)

    async def _on_state(self, payload: dict[str, Any]) -> None:
        """Handle one `state` frame: update local view, maybe dispatch."""
        self.last_state = payload
        trigger = self.pick_trigger(payload)
        if trigger is None:
            return
        log.info("watch: trigger source=%s goal=%r", trigger.source, trigger.goal)
        self.agent_status = "busy"
        loop = asyncio.get_running_loop()
        try:
            rc = await loop.run_in_executor(None, self.dispatch_fn, trigger.goal)
            log.info("watch: cycle done rc=%d", rc)
        except Exception as exc:  # noqa: BLE001 - never let dispatch take down the pump
            log.exception("watch: dispatch crashed (%s)", exc)
        finally:
            self.agent_status = "idle"
            self.note_dispatch(trigger)

    async def _message_pump(self) -> None:
        """Drain `ws.messages()` until the connection ends or `_stop` fires."""
        assert self._stop is not None
        async for msg in self.ws.messages():
            if self._stop.is_set():
                break
            log.debug("watch: ws frame type=%s", msg.type)
            if msg.type == "state":
                await self._on_state(msg.payload)
            elif msg.type == "event":
                # Events are consumed by `tbot listen` subscribers; the
                # connector logs them so an operator running `tbot watch -v`
                # can see traffic flow without a second subscription.
                log.info("watch: event %s", msg.payload.get("event") or msg.payload)
            elif msg.type == "error":
                log.warning("watch: server error: %s", msg.payload)
            elif msg.type == "pong":
                log.debug("watch: pong")
            else:
                log.debug("watch: unknown frame type=%s", msg.type)
            if self._should_exit:
                self._stop.set()
                break

    async def run(self) -> int:
        """Connect, then run the heartbeat and message-pump tasks concurrently."""
        self._stop = asyncio.Event()
        try:
            await self.ws.connect()
        except Exception as exc:  # noqa: BLE001
            log.error("watch: initial WS connect failed: %s", exc)
            return 1

        hb_task = asyncio.create_task(self._heartbeat_task(), name="watch-heartbeat")
        pump_task = asyncio.create_task(self._message_pump(), name="watch-pump")
        try:
            done, pending = await asyncio.wait(
                {hb_task, pump_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    log.warning("watch: task %s raised %s", t.get_name(), exc)
        finally:
            self._stop.set()
            with contextlib.suppress(Exception):
                await self.ws.close()
        return 0

    def stop(self) -> None:
        """Signal the loop to exit at the next message-pump iteration."""
        if self._stop is not None:
            self._stop.set()


# ---------------------------------------------------------------------------
# Default WS client wiring
# ---------------------------------------------------------------------------


def _default_ws_client(
    host: str, ws_port: int, *, auth_token: str | None,
) -> WsClientProtocol:
    """Return a `TimberbotWsClient` instance or a thin local fallback.

    Prefers `timberbot.api.wsclient.TimberbotWsClient` (#29). If that module
    isn't on PYTHONPATH yet (sibling unit hasn't merged), we instantiate a
    minimal `_AiohttpWsClient` defined below. The two satisfy the same
    `WsClientProtocol`, so the rest of the connector doesn't notice.
    """
    try:
        from timberbot.api.wsclient import TimberbotWsClient  # type: ignore[import-not-found]
    except ImportError:
        return _AiohttpWsClient(host=host, ws_port=ws_port, auth_token=auth_token)
    return TimberbotWsClient(host=host, ws_port=ws_port, auth_token=auth_token)


class _AiohttpWsClient:
    """Local fallback used until Unit 2 (#29) lands.

    Implements `WsClientProtocol` against `aiohttp.ClientSession.ws_connect()`.
    Reconnects via `exp_backoff(1s→30s)`. When `TimberbotWsClient` lands, this
    can be deleted — the surface matches.
    """

    def __init__(self, *, host: str, ws_port: int, auth_token: str | None) -> None:
        self.host = host
        self.ws_port = ws_port
        self.auth_token = auth_token
        self._session = None  # type: ignore[assignment]
        self._ws = None  # type: ignore[assignment]
        self._closed = False

    async def connect(self) -> None:
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession()
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        url = f"ws://{self.host}:{self.ws_port}/api/ws"
        attempt = 0
        while not self._closed:
            try:
                self._ws = await self._session.ws_connect(url, headers=headers or None)
                return
            except Exception as exc:  # noqa: BLE001
                delay = exp_backoff(attempt)
                log.warning("watch: ws connect failed (%s); retry in %.1fs", exc, delay)
                attempt += 1
                await asyncio.sleep(delay)

    async def send_message(self, type: str, payload: dict[str, Any]) -> None:  # noqa: A002
        if self._ws is None:
            raise RuntimeError("ws not connected")
        await self._ws.send_str(json.dumps({"type": type, "payload": payload}))

    async def messages(self) -> AsyncIterator[WsMessage]:
        import aiohttp
        attempt = 0
        while not self._closed:
            if self._ws is None:
                await self.connect()
                attempt = 0
            assert self._ws is not None
            try:
                async for raw in self._ws:
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        try:
                            obj = json.loads(raw.data)
                        except json.JSONDecodeError:
                            log.warning("watch: dropping non-JSON WS frame")
                            continue
                        yield WsMessage(
                            type=str(obj.get("type", "")),
                            payload=obj.get("payload") if isinstance(obj.get("payload"), dict) else {},
                        )
                    elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            except Exception as exc:  # noqa: BLE001
                log.warning("watch: ws iteration error (%s)", exc)
            if self._closed:
                break
            self._ws = None
            delay = exp_backoff(attempt)
            log.info("watch: ws closed; reconnecting in %.1fs", delay)
            attempt += 1
            await asyncio.sleep(delay)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None


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
    p.add_argument("--ws-port", dest="ws_port", type=int, default=None,
                   help=("WebSocket port on the mod. Resolution chain: this flag → "
                         "TBOT_WS_PORT env → [client].ws_port in config.toml → 8086."))
    p.add_argument("--autonomous-interval", type=float, default=60.0,
                   help="Seconds between autonomous-mode cycles (default: 60).")
    p.add_argument("--heartbeat-interval", type=float, default=30.0,
                   help="Seconds between WS heartbeat frames (default: 30).")
    p.add_argument("--once", action="store_true",
                   help="Run until a single trigger fires, then exit (useful for debugging).")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Increase log verbosity (-v INFO, -vv DEBUG).")
    return p.parse_args(args)


def run(args: list[str]) -> int:
    """Entry point invoked from `tbot watch`."""
    ns = _parse(args)
    _configure_logging(ns.verbose)

    host, _ = resolve_endpoint()
    ws_port = resolve_ws_port(ns.ws_port)
    auth_token = resolve_auth_token()

    cfg = WatchConfig(
        backend=ns.backend,
        model=ns.model,
        effort=ns.effort,
        prompt_name=ns.prompt_name,
        heartbeat_interval=ns.heartbeat_interval,
        autonomous_interval=ns.autonomous_interval,
        once=ns.once,
        host=host,
        ws_port=ws_port,
        auth_token=auth_token,
    )

    client = TimberbotClient(host=host, auth_token=auth_token, json_mode=True)
    ws_client = _default_ws_client(host, ws_port, auth_token=auth_token)
    loop = WatchLoop(client, cfg, ws_client)

    try:
        return asyncio.run(loop.run())
    except KeyboardInterrupt:
        log.info("watch: interrupted")
        return 0


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
