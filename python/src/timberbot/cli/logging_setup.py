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
    return level
