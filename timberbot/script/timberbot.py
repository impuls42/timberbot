#!/usr/bin/env python
"""Compatibility shim — the real implementation lives in the `tbot` package.

This file used to be a 1883-line monolith. PR 1 of the mod-distribution rework
extracted it into `python/src/tbot/`, installable via `pip install tbot`. The
file is kept around so anything that still calls `python timberbot.py ...`
(in particular the deployed mod copy and `TimberbotAgent.cs`) continues to
work for one release. PR 2 deletes it once the mod stops shipping it.
"""
from __future__ import annotations

import sys

try:
    from tbot import Timberbot, TimberbotError  # noqa: F401  (re-exported for legacy callers)
    from tbot.cli import main
except ImportError as exc:  # pragma: no cover - defensive guidance
    sys.stderr.write(
        "tbot is not installed.\n"
        f"  ImportError: {exc}\n"
        "  install with: pip install tbot   (or, from the repo: pip install -e python/)\n",
    )
    sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
