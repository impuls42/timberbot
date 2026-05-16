"""Small utilities shared across `timberbot.api` and `timberbot.cli`.

This module exists to host helpers that would otherwise create awkward
cross-package imports (e.g. `api/` reaching into `cli/commands/`). Anything
placed here must be dependency-free: no `timberbot.*` imports, no third-party
imports beyond the stdlib.
"""
from __future__ import annotations


def exp_backoff(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff capped at `cap`.

    `attempt=0 → base`; doubles each step until reaching `cap`. Negative
    attempts are floored to `base` (defensive: keeps callers from getting a
    sub-`base` value if they pass an underflowed counter).

    Used by both the HTTP connector (`tbot watch`) and the WS client so the
    operator sees one consistent reconnect cadence.
    """
    if attempt <= 0:
        return base
    return min(cap, base * (2 ** attempt))


__all__ = ["exp_backoff"]
