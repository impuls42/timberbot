"""Tests for `timberbot.cli.logging_setup` — third-party logger adoption.

Two tiers are exercised:

- `_THIRD_PARTY_LOGGERS` is adopted at the user's requested level (so
  `--debug` shows the full telegram lifecycle, including per-update
  processing).
- `_CAPPED_THIRD_PARTY_LOGGERS` is adopted but pinned at a floor that
  prevents `--debug` from unleashing the wire-level transport flood from
  `httpx` / `httpcore` (one INFO line + ~10 DEBUG lines per HTTP request,
  ~6 polling cycles per minute).
"""
from __future__ import annotations

import logging

from timberbot.cli.logging_setup import (
    _CAPPED_THIRD_PARTY_LOGGERS,
    _THIRD_PARTY_LOGGERS,
    configure_logging,
)


def test_telegram_and_uvicorn_loggers_match_verbosity():
    """Lifecycle loggers follow the verbosity flag exactly — at --debug we
    want every `telegram.ext.*` line so 'I sent a /prompt and nothing
    happened' is diagnosable."""
    configure_logging(verbosity=2)  # --debug

    for name in ("telegram", "telegram.ext", "telegram.bot",
                 "fastmcp", "uvicorn"):
        assert logging.getLogger(name).level == logging.DEBUG, (
            f"{name} should follow our level (DEBUG) but got "
            f"{logging.getLevelName(logging.getLogger(name).level)}"
        )


def test_httpx_and_httpcore_capped_at_warning_even_at_debug():
    """Transport-level loggers are pinned at WARNING regardless of
    verbosity. At --debug they'd otherwise emit ~10 byte-trace DEBUG
    lines per Telegram poll, drowning the lifecycle signal we actually
    care about."""
    configure_logging(verbosity=2)  # --debug

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING, (
            f"{name} must be capped at WARNING even at --debug; got "
            f"{logging.getLevelName(logging.getLogger(name).level)}"
        )


def test_capped_loggers_track_higher_levels_unchanged():
    """At default WARNING (or quieter), the cap is a no-op — both tiers
    end up at the same level. The cap only matters when the user opts
    into a level *below* the floor."""
    configure_logging(verbosity=0)  # default WARNING

    for name in ("telegram", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING


def test_capped_loggers_inherit_handler():
    """Capped loggers still get the unified formatter — the cap only
    pins the level, not the handler. That way an actual httpx WARNING
    flows through the same `HH:MM:SS name LEVEL message` sink as
    everything else."""
    configure_logging(verbosity=2)

    httpx_logger = logging.getLogger("httpx")
    telegram_logger = logging.getLogger("telegram")
    # Both should have at least one handler (ours).
    assert httpx_logger.handlers
    assert telegram_logger.handlers
    # And neither should propagate (we own their output).
    assert not httpx_logger.propagate
    assert not telegram_logger.propagate


def test_capped_set_excludes_full_adoption_set():
    """Sanity: a logger can't be in both tiers, otherwise the second
    pass would overwrite the first's level setting non-deterministically
    if the iteration order ever changed."""
    assert not (set(_THIRD_PARTY_LOGGERS) & set(_CAPPED_THIRD_PARTY_LOGGERS))
