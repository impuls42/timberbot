"""`tbot start` — kick off the in-mod agent loop via the HTTP API.

This forwards to `/api/agent/start` on the C# mod. The pluggable `tbot agent
run ...` namespace and the local backend dispatch arrive in PR 2.
"""
from __future__ import annotations

import sys

from tbot.api.client import TimberbotClient
from tbot.api.exceptions import TimberbotError
from tbot.formatters.colors import BGRN, DIM, RED, RST


def _parse_args(args: list[str]) -> dict[str, object]:
    parsed: dict[str, object] = {
        "binary": "claude",
        "turns": 5,
        "interval": 10,
        "timeout": 120,
    }
    for a in args:
        if ":" not in a:
            continue
        key, val = a.split(":", 1)
        if key in {"turns", "interval", "timeout"}:
            try:
                parsed[key] = int(val)
            except ValueError:
                pass
        elif key in {"binary", "model", "goal", "command"}:
            parsed[key] = val
    return parsed


def run(args: list[str]) -> int:
    parsed = _parse_args(args)

    bot = TimberbotClient(json_mode=True)
    if not bot.ping():
        print(
            f"  {RED}error: game not reachable. launch first with: tbot launch settlement:<name>{RST}",
            file=sys.stderr,
        )
        return 1

    body = {k: v for k, v in parsed.items() if v not in (None, "")}
    try:
        bot._post("/api/agent/start", body)
    except TimberbotError as e:
        print(f"  {RED}error: {e.error}{RST}", file=sys.stderr)
        return 1

    binary = parsed.get("binary")
    turns = parsed.get("turns")
    interval = parsed.get("interval")
    print(f"  {BGRN}started{RST} binary={binary} turns={turns} interval={interval}s")
    if "command" in parsed:
        print(f"  {DIM}command: {parsed['command']}{RST}")
    print(f"  {DIM}use 'tbot top' to monitor{RST}")
    return 0
