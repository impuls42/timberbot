"""`tbot listen` — reference webhook receiver.

Accepts the batched webhook POSTs documented in `docs/webhooks.md` and emitted
by the C# mod's `TimberbotWebhook.cs`. Useful as:

  * A standalone tool for users who want to see events on stdout.
  * A local listener that the `tbot watch` connector can register with the mod
    via `/api/tbot/register`.

CLI surface::

    tbot listen [--port 9000] [--pretty] [--forward-to PATH_OR_URL] [--quiet]

Endpoints (both accept the same payload — a JSON array of event objects)::

    POST /
    POST /events

Each event object is::

    {"event": "<type>", "day": <int>, "timestamp": <unix-seconds>, "data": <any>}

The handler is intentionally permissive: it accepts a single event object as
well as a list (the canonical shape).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, web

_HTTP_SESSION: web.AppKey[ClientSession] = web.AppKey("http_session", ClientSession)


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


def _normalize_payload(body: Any) -> list[dict[str, Any]]:
    """Accept either a list of events or a single event object.

    Non-dict entries in a list payload are dropped and announced on stderr so
    that the caller can see the discrepancy without the receiver dying.
    """
    if isinstance(body, list):
        good = [e for e in body if isinstance(e, dict)]
        dropped = len(body) - len(good)
        if dropped:
            print(f"listen: dropped {dropped} non-object entr{'y' if dropped == 1 else 'ies'} from batch",
                  file=sys.stderr)
        return good
    if isinstance(body, dict):
        return [body]
    return []


async def _forward(events: list[dict[str, Any]], target: str, session: ClientSession | None) -> None:
    """Forward a batch to `target`. file:// or bare paths append; http(s):// POSTs."""
    if target.startswith(("http://", "https://")):
        if session is None:
            return
        try:
            async with session.post(target, json=events, timeout=10) as resp:
                await resp.read()
        except Exception as exc:  # pragma: no cover - network errors logged, not fatal
            print(f"listen: forward error: {exc}", file=sys.stderr)
        return

    # file:// or bare path → append one JSON line per event. Errors here
    # (full disk, missing permissions) are logged rather than propagated so a
    # transient sink failure doesn't bubble a 500 back to the mod.
    path = Path(target.removeprefix("file://")).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
    except OSError as exc:
        print(f"listen: forward error: {exc}", file=sys.stderr)


def build_app(*, pretty: bool = False, forward_to: str | None = None, quiet: bool = False) -> web.Application:
    """Build the aiohttp application.

    Exposed for tests so they can mount it on `aiohttp.test_utils.TestServer`
    or drive it via a real TCP socket on a chosen port.
    """
    app = web.Application()

    needs_session = bool(forward_to and forward_to.startswith(("http://", "https://")))

    async def _on_startup(app: web.Application) -> None:
        if needs_session:
            app[_HTTP_SESSION] = ClientSession()

    async def _on_cleanup(app: web.Application) -> None:
        session = app.get(_HTTP_SESSION)
        if session is not None:
            await session.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    async def handle(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        events = _normalize_payload(body)
        if not quiet:
            for ev in events:
                print(_format_pretty(ev) if pretty else json.dumps(ev))

        if forward_to and events:
            await _forward(events, forward_to, request.app.get(_HTTP_SESSION))

        return web.json_response({"received": len(events)})

    app.router.add_post("/", handle)
    app.router.add_post("/events", handle)
    return app


def _parse(args: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tbot listen", description="Reference webhook receiver.")
    p.add_argument("--port", type=int, default=9000, help="TCP port to listen on (default 9000).")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    p.add_argument("--pretty", action="store_true",
                   help="Print one human-friendly line per event instead of raw JSON.")
    p.add_argument("--forward-to", default=None, metavar="PATH_OR_URL",
                   help="Append each event to a file (file:// or bare path) or POST the batch to a URL (http(s)://).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stdout (only --forward-to receives events).")
    return p.parse_args(args)


def run(args: list[str]) -> int:
    ns = _parse(args)
    app = build_app(pretty=ns.pretty, forward_to=ns.forward_to, quiet=ns.quiet)
    if not ns.quiet:
        print(f"listening on http://{ns.host}:{ns.port} (POST / or /events)")
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        web.run_app(app, host=ns.host, port=ns.port, print=None, handle_signals=True)
    return 0
