"""Small table primitives used by the dashboard renderer."""
from __future__ import annotations

import re

from tbot.formatters.colors import BGRN, BOLD, BRED, BYEL, DIM, RST

WIDTH = 86

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def cv(val: float, warn: float, crit: float, fmt: str = ".0f") -> str:
    """Color a value: green/yellow/red based on warn/crit thresholds."""
    c = BRED if val < crit else BYEL if val < warn else BGRN
    return f"{c}{BOLD}{val:{fmt}}{RST}"


def bar(cur: float, mx: float, w: int = 12) -> str:
    """Progress bar with gradient."""
    if mx <= 0:
        return f"{DIM}{'░' * w}{RST}"
    ratio = max(0.0, min(cur / mx, 1.0))
    filled = int(ratio * w)
    c = BRED if ratio < 0.25 else BYEL if ratio < 0.5 else BGRN
    return f"{c}{'█' * filled}{DIM}{'░' * (w - filled)}{RST}"


def hline(width: int = WIDTH) -> str:
    return f" {DIM}{'─' * width}{RST}"


def row(left: str, right: str | None = None, split: int = 43) -> str:
    """Two-column row (no side borders). ANSI codes in `left` don't affect alignment."""
    if right is None:
        return f"  {left}"
    plain_l = _ANSI_RE.sub("", left)
    pad_l = max(0, split - len(plain_l))
    return f"  {left}{' ' * pad_l}  {right}"
