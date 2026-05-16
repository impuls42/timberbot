"""`tbot listen` — pure WebSocket event subscriber.

Connects to the mod's `/api/ws` channel (PR series rework — heartbeat polling
and outbound HTTP webhooks have been collapsed into a single long-lived
WebSocket). Filters inbound frames for `type == "event"` and renders them with
the same `--pretty` / `--forward-to` / `--quiet` surface the previous
HTTP-inbound implementation exposed.

CLI surface::

    tbot listen [--pretty] [--forward-to PATH_OR_URL] [--quiet]
                [--ws-port N] [--host HOST] [--auth-token T]

Each event frame on the wire looks like::

    {"type": "event", "event": "<name>", "day": <int>,
     "timestamp": <unix-seconds>, "data": <any>}

Non-event frames (heartbeats, server-side acks, etc.) are dropped silently.

Host / port / auth-token resolution piggybacks on the existing precedence
chain in `timberbot.settings` so the new `--ws-port` flag composes with the
global `--host=`, `--auth-token=`, `TBOT_HOST` / `TBOT_AUTH_TOKEN` env, and
`[client]` config-file overrides exactly like every other client surface.

The WS connection auto-reconnects on close with capped exponential backoff
(shared schedule with `tbot watch` via `watch.exp_backoff`). The sibling unit
(#29) ships a typed `TimberbotWsClient`; until that lands this module talks
directly to `aiohttp.ClientSession.ws_connect`. Switching to the typed
client is a follow-up — the public CLI surface and the JSON frame shape are
stable.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from timberbot.cli.commands.watch import exp_backoff, resolve_ws_port
from timberbot.settings import resolve_auth_token, resolve_endpoint


def _format_pretty(event: dict[str, Any]) -> str:
    """Render one event as a single human-friendly line."""
    name = event.get("event", "?")
    day = event.get("day", "?")
    ts = event.get("timestamp")
    when = ""
    if isinstance(ts, int):
        when = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%H:%M:%S")
    data = event.get("data")
    tag = "" if data in (None, {}, []) else f"  {json.dumps(data, sort_keys=True)}"
    return f"[day {day} {when}] {name}{tag}".rstrip()


def _is_event_frame(frame: Any) -> bool:
    """True if `frame` is an event-shaped object (`type == 'event'`)."""
    return isinstance(frame, dict) and frame.get("type") == "event"


async def _forward(
    event: dict[str, Any],
    target: str,
    session: aiohttp.ClientSession | None,
) -> None:
    """Forward a single event to `target`.

    `file://` or bare path → append one JSON line. `http(s)://` → POST a
    1-element batch (the old webhook batch shape, kept so downstream
    collectors don't need to special-case the WS migration).
    """
    if target.startswith(("http://", "https://")):
        if session is None:
            return
        try:
            async with session.post(target, json=[event], timeout=10) as resp:
                await resp.read()
        except Exception as exc:  # pragma: no cover - logged, not fatal
            print(f"listen: forward error: {exc}", file=sys.stderr)
        return

    path = Path(target.removeprefix("file://")).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        print(f"listen: forward error: {exc}", file=sys.stderr)


async def handle_frame(
    frame: Any,
    *,
    pretty: bool = False,
    quiet: bool = False,
    forward_to: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """Render & forward one decoded JSON frame.

    Returns True iff the frame was an event and was emitted. Non-event frames
    are dropped silently so server-side bookkeeping doesn't leak into the
    user-facing stream.
    """
    if not _is_event_frame(frame):
        return False

    if not quiet:
        print(_format_pretty(frame) if pretty else json.dumps(frame))

    if forward_to:
        await _forward(frame, forward_to, session)
    return True


def _ws_url(host: str, ws_port: int) -> str:
    # Plain ws:// only; the mod listens on localhost by default and the
    # auth-token header is the security boundary. `wss://` support (for
    # remote deployments behind a TLS proxy) is a follow-up — the typed
    # client landing in #29 is the natural place to add it.
    return f"ws://{host}:{ws_port}/api/ws"


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


_TERMINAL_WS_TYPES = frozenset({
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.ERROR,
})


async def _consume(
    ws: aiohttp.ClientWebSocketResponse,
    *,
    pretty: bool,
    quiet: bool,
    forward_to: str | None,
    forward_session: aiohttp.ClientSession | None,
) -> None:
    """Drain `ws` until it closes.

    Malformed TEXT frames hit stderr but don't tear the channel down — the
    mod might emit a transient bad frame during a serializer hotfix and
    we'd rather log it than drop the subscription.
    """
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                frame = json.loads(msg.data)
            except json.JSONDecodeError as exc:
                print(f"listen: malformed frame: {exc}", file=sys.stderr)
                continue
            await handle_frame(
                frame,
                pretty=pretty,
                quiet=quiet,
                forward_to=forward_to,
                session=forward_session,
            )
        elif msg.type in _TERMINAL_WS_TYPES:
            break


async def subscribe(
    *,
    host: str,
    ws_port: int,
    auth_token: str | None = None,
    pretty: bool = False,
    quiet: bool = False,
    forward_to: str | None = None,
    max_attempts: int | None = None,
    backoff_base: float = 1.0,
    backoff_cap: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_factory: Callable[[], aiohttp.ClientSession] = aiohttp.ClientSession,
) -> int:
    """Subscribe to `ws://host:ws_port/api/ws` and stream event frames.

    Auto-reconnects on close with capped exponential backoff. `max_attempts`
    caps total `ws_connect` calls; production callers leave it `None` for
    "loop forever". Tests pass a small value so the coroutine terminates
    deterministically.

    Returns 0 on clean exit.
    """
    url = _ws_url(host, ws_port)
    headers = _auth_headers(auth_token)
    needs_http_forward = bool(forward_to and forward_to.startswith(("http://", "https://")))

    # `connect_count` drives `max_attempts`. `backoff_step` is the
    # consecutive-failure counter and resets on every successful upgrade.
    connect_count = 0
    backoff_step = 0
    while True:
        if max_attempts is not None and connect_count >= max_attempts:
            return 0
        connect_count += 1

        async with session_factory() as session:
            forward_session = session if needs_http_forward else None
            try:
                async with session.ws_connect(url, headers=headers) as ws:
                    if not quiet:
                        print(f"listen: connected to {url}", file=sys.stderr)
                    backoff_step = 0
                    await _consume(
                        ws,
                        pretty=pretty,
                        quiet=quiet,
                        forward_to=forward_to,
                        forward_session=forward_session,
                    )
            except (aiohttp.ClientError, ConnectionError, OSError) as exc:
                # `ConnectionError` covers `BrokenPipeError` /
                # `ConnectionResetError` from inside `_consume` (e.g. a
                # `print` to a torn-down pipe) — those should reconnect,
                # not kill the subscriber.
                if not quiet:
                    print(f"listen: connect failed ({exc}); retrying", file=sys.stderr)

        if max_attempts is not None and connect_count >= max_attempts:
            return 0
        delay = exp_backoff(backoff_step, base=backoff_base, cap=backoff_cap)
        backoff_step += 1
        if not quiet:
            print(f"listen: reconnect in {delay:.1f}s", file=sys.stderr)
        await sleep(delay)


def _parse(args: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tbot listen",
                                description="WebSocket event subscriber.")
    p.add_argument("--pretty", action="store_true",
                   help="Print one human-friendly line per event instead of raw JSON.")
    p.add_argument("--forward-to", default=None, metavar="PATH_OR_URL",
                   help=("Append each event as JSON to a file (file:// or bare path) "
                         "or POST it as a 1-element batch to a URL (http(s)://)."))
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stdout output (only --forward-to receives events).")
    p.add_argument("--ws-port", type=int, default=None,
                   help="WebSocket port on the mod (defaults to the resolved HTTP port).")
    p.add_argument("--host", default=None,
                   help="Mod host. Overrides TBOT_HOST and config.toml [client].host.")
    p.add_argument("--auth-token", default=None,
                   help="Bearer token. Overrides TBOT_AUTH_TOKEN and config.toml [client].auth_token.")
    return p.parse_args(args)


def run(args: list[str]) -> int:
    """Entry point for `tbot listen`.

    Resolves host/port/auth-token via the shared precedence chain (CLI → env
    → user config.toml → mod settings.json → defaults). `--ws-port` is the
    one WS-specific knob; when absent `resolve_ws_port` returns the default
    8086 (the mod runs the WS listener on its own port, not the HTTP one).
    """
    ns = _parse(args)
    host, _http_port = resolve_endpoint(ns.host, None)
    ws_port = resolve_ws_port(ns.ws_port)
    token = resolve_auth_token(ns.auth_token)

    if not ns.quiet:
        print(f"listen: subscribing to ws://{host}:{ws_port}/api/ws", file=sys.stderr)

    try:
        return asyncio.run(subscribe(
            host=host,
            ws_port=ws_port,
            auth_token=token,
            pretty=ns.pretty,
            quiet=ns.quiet,
            forward_to=ns.forward_to,
        ))
    except KeyboardInterrupt:
        return 0
