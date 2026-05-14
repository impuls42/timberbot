"""Snapshot-style tests for the pure formatters."""
from __future__ import annotations

import re

from tbot.formatters.dashboard import render_top
from tbot.formatters.map import render_map
from tbot.formatters.tables import bar, cv, hline, row

ANSI = re.compile(r"\033\[[0-9;]*m")


def strip(s: str) -> str:
    return ANSI.sub("", s)


def test_cv_color_buckets():
    # cv uses bright variants: BRED=91, BYEL=93, BGRN=92.
    assert "\033[91m" in cv(0, warn=10, crit=5)
    assert "\033[93m" in cv(7, warn=10, crit=5)
    assert "\033[92m" in cv(20, warn=10, crit=5)


def test_bar_renders_full_at_full():
    rendered = strip(bar(10, 10, w=4))
    assert rendered.startswith("█" * 4)


def test_bar_renders_empty_at_zero():
    rendered = strip(bar(0, 10, w=4))
    assert "░" * 4 in rendered


def test_bar_handles_zero_max():
    rendered = strip(bar(0, 0, w=4))
    assert rendered == "░" * 4


def test_row_two_column_alignment_is_ansi_aware():
    left = "\033[1mhello\033[0m"
    right = "world"
    line = strip(row(left, right, split=10))
    assert "hello" in line
    assert "world" in line
    assert line.index("world") > line.index("hello")


def test_hline_includes_dashes():
    assert "─" in hline()


def test_render_map_minimal_no_tiles():
    rendered = render_map({"tiles": []}, x1=0, y1=0, x2=2, y2=2)
    plain = strip(rendered)
    # axis row is the last informative line; expect numbers 0..2
    assert "012" in plain


def test_render_map_renders_water_tiles():
    tiles = {
        "tiles": [
            {"x": 0, "y": 0, "terrain": 5, "water": 1, "occupants": []},
            {"x": 1, "y": 0, "terrain": 5, "water": 0, "occupants": []},
        ],
    }
    rendered = render_map(tiles, x1=0, y1=0, x2=1, y2=0)
    plain = strip(rendered)
    assert "~" in plain  # water symbol
    assert "5" in plain  # terrain digit (5 % 10)


def test_render_top_returns_unreachable_message_for_none():
    out = render_top(None)
    assert "not reachable" in strip(out)


def test_render_top_includes_population_block():
    summary = {
        "time": {"dayNumber": 5, "dayProgress": 0.5},
        "weather": {"isHazardous": False, "temperateWeatherDuration": 12, "cycleDay": 3},
        "districts": [
            {
                "name": "main",
                "population": {"adults": 10, "children": 4, "bots": 1},
                "resources": {"Water": {"available": 200}, "Log": 50},
                "housing": {"occupiedBeds": 14, "totalBeds": 20, "homeless": 0},
                "employment": {"assigned": 12, "vacancies": 14, "unemployed": 2},
                "wellbeing": {"average": 18, "miserable": 0, "critical": 0},
            },
        ],
        "wellbeing": {"average": 18, "miserable": 0, "critical": 0, "categories": []},
    }
    out = strip(render_top(summary))
    assert "Day 5" in out
    assert "15 beavers" in out  # 10+4+1
    assert "Beds 14/20" in out
    assert "DISTRICTS" in out
    assert "main" in out
