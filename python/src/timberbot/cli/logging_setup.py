"""Centralized logging configuration for the `tbot` CLI.

Every subcommand needs the same thing: a stderr handler attached to the
`timberbot` package logger at a verbosity derived from `-v` / `--debug`. This
module is the single source so we don't keep duplicating `_configure_logging`
across `watch.py`, `serve.py`, and now the global dispatcher.

Verbosity scale:
    0  -> WARNING  (default; user sees output + errors only)
    1  -> INFO     (`-v`; per-request URLs, resolved endpoint, status)
    2+ -> DEBUG    (`-vv` or `--debug`; bodies, headers redacted)

Idempotent: calling twice doesn't double-attach the handler; it just adjusts
the level. Tests rely on this so they can configure once per session.
"""
from __future__ import annotations

import logging
import os
import sys

# Module-level so tests can clear it between runs if they need to.
_HANDLER: logging.Handler | None = None


def level_from_verbosity(verbosity: int, *, debug: bool = False) -> int:
    """Map (verbosity count, debug flag) to a `logging` level constant."""
    if debug or verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return logging.INFO
    return logging.WARNING


def configure_logging(verbosity: int = 0, *, debug: bool = False) -> int:
    """Attach (once) a stderr handler to the `timberbot` package logger.

    Returns the resolved level so callers can introspect what got applied
    (handy in tests). The level is also applied to the root `tbot` console
    logger so messages from `timberbot.api.*`, `timberbot.cli.*`, and
    `timberbot.agent.*` all flow through the same sink.

    `TBOT_DEBUG=1` in the environment forces DEBUG regardless of the flag —
    useful when an agent is shelling out to `tbot` and you can't easily add
    `-vv` to every call.
    """
    global _HANDLER

    if os.environ.get("TBOT_DEBUG", "").strip() in ("1", "true", "True", "yes"):
        debug = True

    level = level_from_verbosity(verbosity, debug=debug)

    pkg = logging.getLogger("timberbot")
    if _HANDLER is None:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s",
                              datefmt="%H:%M:%S")
        )
        # Avoid double-attach if a host (tests, a wrapping CLI) already added
        # a handler to the same logger.
        if not pkg.handlers:
            pkg.addHandler(handler)
        _HANDLER = handler
    else:
        # Refresh the stream reference so the handler tracks the *current*
        # sys.stderr. Pytest's `capsys` swaps stderr per-test, so a cached
        # stream from a previous test would dangle. Production callers see
        # no change because sys.stderr is stable for a CLI invocation.
        _HANDLER.stream = sys.stderr

    pkg.setLevel(level)
    _adopt_third_party_loggers(level)
    return level


# `tbot serve` pulls in fastmcp (RichHandler with timestamp + file:line) and
# uvicorn (`INFO:    ` prefix). Left alone they print in two extra formats
# alongside our `HH:MM:SS name LEVEL message` line, which is jarring. Strip
# their handlers, disable propagation guards, and route them through our
# handler so every line in `tbot serve` output looks the same.
#
# python-telegram-bot is the other big source — its `telegram.*` loggers
# explain the polling/dispatch lifecycle (Application start, getUpdates
# round-trips, allowlist filtering, message handlers). Without adoption,
# `tbot serve` is silent about Telegram traffic even at --debug, which
# makes "I sent a /prompt and nothing happened" impossible to diagnose.
_THIRD_PARTY_LOGGERS = (
    "fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access",
    "telegram", "telegram.ext", "telegram.bot",
)


# Adopted but capped: same formatter + handler unification, but never
# allowed to drop below the floor regardless of how high the user's
# verbosity climbs. The transport stack (httpx → httpcore) under
# python-telegram-bot logs one INFO line per HTTP request and ~10 DEBUG
# lines per request tracing the raw socket/TLS/H11 state machine. With
# 10-second long-polling that's a 6/min INFO drip + a 60/min DEBUG flood,
# drowning the genuinely useful `telegram.ext.*` lifecycle messages.
#
# Pin to WARNING so we only hear from the transport on errors. Anyone
# debugging the wire can still opt back in manually:
#
#     import logging; logging.getLogger("httpcore").setLevel(logging.DEBUG)
_CAPPED_THIRD_PARTY_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
}


def _adopt_third_party_loggers(level: int) -> None:
    if _HANDLER is None:
        return
    for name in _THIRD_PARTY_LOGGERS:
        _adopt_logger(name, level)
    for name, floor in _CAPPED_THIRD_PARTY_LOGGERS.items():
        _adopt_logger(name, max(level, floor))


def _adopt_logger(name: str, level: int) -> None:
    """Strip the named logger's handlers, attach ours, pin to `level`.

    Internal helper — both the full-adoption and the capped-adoption paths
    do the same descriptor surgery; only the level differs.
    """
    assert _HANDLER is not None
    lg = logging.getLogger(name)
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.addHandler(_HANDLER)
    lg.propagate = False
    lg.setLevel(level)
