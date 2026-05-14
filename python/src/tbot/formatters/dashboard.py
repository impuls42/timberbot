"""Live colony dashboard renderer.

Pure: takes a summary payload and returns the multi-line string. The CLI
`top` command owns the polling loop and writes to stdout.
"""
from __future__ import annotations

from typing import Any

from tbot.formatters.colors import (
    BBLU,
    BCYN,
    BGRN,
    BMAG,
    BOLD,
    BRED,
    BYEL,
    DIM,
    RED,
    RST,
)
from tbot.formatters.tables import WIDTH, bar, cv, hline, row

_EDIBLE = [
    "Berries", "Kohlrabi", "Bread", "Carrot", "CornRation", "AlgaeRation",
    "EggplantRation", "FermentedSoybean", "FermentedMushroom", "FermentedCassava",
    "Coffee", "MangroveFruit",
]
_RAW_CROPS = [
    "Soybean", "Corn", "Sunflower", "Eggplant", "Algae", "Cassava", "Mushroom",
]


def render_top(
    summary: dict[str, Any] | None,
    *,
    wellbeing_data: dict[str, Any] | None = None,
    trees_data: list[dict[str, Any]] | None = None,
    crops_data: list[dict[str, Any]] | None = None,
    interval: int = 5,
    agent_data: dict[str, Any] | None = None,
    agent_turns: int = 5,
) -> str:
    """Render the live dashboard. Returns the entire frame as one string."""
    if not summary:
        return f"\n {RED}-- game not reachable --{RST}\n"

    lines: list[str] = []

    t = summary.get("time", {})
    w = summary.get("weather", {})
    day = t.get("dayNumber", 0)
    hazardous = w.get("isHazardous", False)
    temp_len = w.get("temperateWeatherDuration", 0)
    haz_len = w.get("hazardousWeatherDuration", 0)
    cday = w.get("cycleDay", 0)
    remaining = temp_len + haz_len - cday + 1 if hazardous else temp_len - cday + 1

    day_progress = t.get("dayProgress", 0)
    season_str = f"{BRED}{BOLD}DROUGHT{RST}" if hazardous else f"{BGRN}Temperate{RST}"
    day_bar = bar(day_progress, 1.0, 8)
    day_str = (
        f"Day {BCYN}{BOLD}{day}{RST} {day_bar}  {season_str} "
        f"{DIM}{cday}/{temp_len}+{haz_len}{RST} ({BOLD}{remaining}d{RST})"
    )

    lines.append(f" {DIM}{'─' * WIDTH}{RST}")
    lines.append(row(f"{BCYN}{BOLD}Timberbot API{RST}                            {day_str}"))
    lines.append(hline())

    districts = summary.get("districts", [])
    total_adults = sum(d.get("population", {}).get("adults", 0) for d in districts)
    total_children = sum(d.get("population", {}).get("children", 0) for d in districts)
    total_bots = sum(d.get("population", {}).get("bots", 0) for d in districts)
    total_pop = total_adults + total_children + total_bots

    resources: dict[str, int] = {}
    for d in districts:
        for good, val in d.get("resources", {}).items():
            amt = val.get("available", val) if isinstance(val, dict) else val
            resources[good] = resources.get(good, 0) + amt

    occ_beds = sum(d.get("housing", {}).get("occupiedBeds", 0) for d in districts)
    tot_beds = sum(d.get("housing", {}).get("totalBeds", 0) for d in districts)
    assigned = sum(d.get("employment", {}).get("assigned", 0) for d in districts)
    vacancies = sum(d.get("employment", {}).get("vacancies", 0) for d in districts)
    unemployed = sum(d.get("employment", {}).get("unemployed", 0) for d in districts)
    wb_obj = summary.get("wellbeing", {})
    wb_avg = wb_obj.get("average", 0) if isinstance(wb_obj, dict) else 0
    critical = wb_obj.get("critical", 0) if isinstance(wb_obj, dict) else 0

    pop_parts = f"{BOLD}{total_adults}{RST} adults  {BOLD}{total_children}{RST} children"
    if total_bots:
        pop_parts += f"  {BOLD}{total_bots}{RST} bots"

    homeless = sum(d.get("housing", {}).get("homeless", 0) for d in districts)
    miserable = wb_obj.get("miserable", 0) if isinstance(wb_obj, dict) else 0
    science = summary.get("science", 0)
    idle_c = BRED if unemployed == 0 else BGRN if unemployed <= 4 else BYEL
    crit_str = f"  {BRED}{BOLD}● {critical} critical{RST}" if critical > 0 else ""
    homeless_str = f"  {BRED}{BOLD}{homeless} homeless{RST}" if homeless > 0 else ""
    miserable_str = f"  {BYEL}{miserable} miserable{RST}" if miserable > 0 else ""

    lines.append(row(
        f"{BCYN}{BOLD}{total_pop}{RST} beavers  {DIM}({pop_parts}{DIM}){RST}",
        f"Beds {BOLD}{occ_beds}{RST}/{tot_beds}  Workers {BOLD}{assigned}{RST}/{vacancies}  Idle {idle_c}{BOLD}{unemployed}{RST}",
    ))
    lines.append(row(
        f"Wellbeing {bar(wb_avg, 77, 20)} {cv(wb_avg, 8, 4, '.1f')}/77{crit_str}{miserable_str}{homeless_str}"
    ))
    lines.append(hline())

    total_food = sum(resources.get(g, 0) for g in _EDIBLE)
    total_water = resources.get("Water", 0)
    food_days = round(total_food / total_pop, 1) if total_pop > 0 else 0
    water_days = round(total_water / (total_pop * 2), 1) if total_pop > 0 else 0

    food_items = [(g, resources.get(g, 0)) for g in _EDIBLE if resources.get(g, 0) > 0]

    wb_cats: list[tuple[str, int, int]] = []
    wb_source = wb_obj.get("categories", []) if isinstance(wb_obj, dict) and "categories" in wb_obj else (
        wellbeing_data.get("categories", []) if wellbeing_data and isinstance(wellbeing_data, dict) else []
    )
    for cat in wb_source:
        wb_cats.append((cat.get("group", "?"), cat.get("current", 0), cat.get("max", 0)))

    left_lines = [
        f"{BCYN}{BOLD}FOOD{RST}  {cv(food_days, 3, 1, '.1f')} days  {DIM}({total_food} total){RST}"
    ]
    for i, (g, amt) in enumerate(food_items):
        branch = "└─" if i == len(food_items) - 1 else "├─"
        left_lines.append(f"  {DIM}{branch}{RST} {g:16s} {BOLD}{amt:>5}{RST}")

    left_lines.append(f"{BCYN}{BOLD}WATER{RST} {cv(water_days, 2, 0.5, '.1f')} days  {BBLU}{BOLD}{total_water}{RST}")
    left_lines.append("")

    right_lines = [f"{BCYN}{BOLD}WELLBEING{RST}"]
    for g, cur, mx in wb_cats:
        right_lines.append(f"{g:13s} {bar(cur, mx, 10)} {cv(cur, mx * 0.5, mx * 0.1, '.1f')}{DIM}/{mx:.0f}{RST}")

    max_rows = max(len(left_lines), len(right_lines))
    for i in range(max_rows):
        ll = left_lines[i] if i < len(left_lines) else ""
        rr = right_lines[i] if i < len(right_lines) else ""
        lines.append(row(ll, rr))

    lines.append(hline())

    mat_lines = [f"{BCYN}{BOLD}MATERIALS{RST}"]
    for good in ["Log", "Plank", "Gear", "ScrapMetal", "MetalPart"]:
        if good in resources:
            mat_lines.append(f"  {good:16s} {BOLD}{resources[good]:>5}{RST}")
    mat_lines.append(f"  {'Science':16s} {BCYN}{BOLD}{science:>5}{RST}")

    alerts_obj = summary.get("alerts", {})
    alert_lines = [f"{BCYN}{BOLD}ALERTS{RST}"]
    if isinstance(alerts_obj, dict):
        for k, v in alerts_obj.items():
            if v > 0:
                alert_lines.append(f"  {BYEL}⚠ {v} {k}{RST}")
    if len(alert_lines) == 1:
        alert_lines.append(f"  {BGRN}● all clear{RST}")

    max_rows = max(len(mat_lines), len(alert_lines))
    for i in range(max_rows):
        ll = mat_lines[i] if i < len(mat_lines) else ""
        rr = alert_lines[i] if i < len(alert_lines) else ""
        lines.append(row(ll, rr))

    trees_obj = summary.get("trees", {})
    tree_species = trees_obj.get("species", []) if isinstance(trees_obj, dict) else []
    if tree_species:
        tree_counts: dict[str, dict[str, int]] = {}
        for s in tree_species:
            n = s.get("name", "")
            tree_counts[n] = {
                "marked_grown": s.get("markedGrown", 0),
                "unmarked_grown": s.get("unmarkedGrown", 0),
                "seedling": s.get("seedling", 0),
            }
    elif trees_data and isinstance(trees_data, list):
        tree_counts = {}
        for t in trees_data:
            n = t.get("name", "")
            if n not in tree_counts:
                tree_counts[n] = {"marked_grown": 0, "unmarked_grown": 0, "seedling": 0}
            if not t.get("alive"):
                continue
            if t.get("grown"):
                if t.get("marked"):
                    tree_counts[n]["marked_grown"] += 1
                else:
                    tree_counts[n]["unmarked_grown"] += 1
            elif t.get("marked"):
                tree_counts[n]["seedling"] += 1
    else:
        tree_counts = {}
    if tree_counts:
        lines.append(hline())
        tree_left = [f"{BCYN}{BOLD}TREES{RST}"]
        total_chop = sum(c["marked_grown"] for c in tree_counts.values())
        total_unmarked = sum(c["unmarked_grown"] for c in tree_counts.values())
        total_seed = sum(c["seedling"] for c in tree_counts.values())
        tree_left.append(
            f"  {BGRN}{BOLD}{total_chop}{RST} choppable  {DIM}{total_unmarked} unmarked  {total_seed} seedlings{RST}"
        )
        for name in sorted(tree_counts, key=lambda n: tree_counts[n]["marked_grown"], reverse=True):
            c = tree_counts[name]
            if c["marked_grown"] + c["unmarked_grown"] + c["seedling"] > 0:
                tree_left.append(
                    f"  {DIM}{name:10s}{RST} {BGRN}{BOLD}{c['marked_grown']:>4}{RST} marked  "
                    f"{DIM}{c['unmarked_grown']} free  {c['seedling']} growing{RST}"
                )
        for ll in tree_left:
            lines.append(row(ll, ""))

    crops_obj = summary.get("crops", {})
    crop_species = crops_obj.get("species", []) if isinstance(crops_obj, dict) else []
    if crop_species:
        crop_counts: dict[str, dict[str, int]] = {}
        for s in crop_species:
            n = s.get("name", "")
            crop_counts[n] = {
                "alive": s.get("ready", 0) + s.get("growing", 0),
                "grown": s.get("ready", 0),
            }
    elif crops_data and isinstance(crops_data, list):
        crop_counts = {}
        for t in crops_data:
            name = t.get("name", "")
            if name not in crop_counts:
                crop_counts[name] = {"alive": 0, "grown": 0}
            if t.get("alive"):
                crop_counts[name]["alive"] += 1
            if t.get("grown"):
                crop_counts[name]["grown"] += 1
    else:
        crop_counts = {}
    if crop_counts:
        lines.append(hline())
        crop_left = [f"{BCYN}{BOLD}CROPS{RST}  {DIM}(in ground){RST}"]
        items = sorted(crop_counts.items(), key=lambda x: x[1]["alive"], reverse=True)
        for name, c in items:
            grown_c = BGRN if c["grown"] > 0 else DIM
            crop_left.append(
                f"  {name:14s} {grown_c}{BOLD}{c['grown']:>4}{RST} ready  "
                f"{DIM}{c['alive'] - c['grown']} growing{RST}"
            )
        for ll in crop_left:
            lines.append(row(ll, ""))

    if len(districts) > 0:
        lines.append(hline())
        lines.append(row(f"{BCYN}{BOLD}DISTRICTS{RST}"))
        for d in districts:
            name = d.get("name", "?")
            pop = d.get("population", {})
            dpop = pop.get("adults", 0) + pop.get("children", 0) + pop.get("bots", 0)
            dres = d.get("resources", {})
            dwater = dres.get("Water", 0)
            dw = dwater.get("available", 0) if isinstance(dwater, dict) else dwater
            dlog = dres.get("Log", 0)
            dl = dlog.get("available", 0) if isinstance(dlog, dict) else dlog
            lines.append(row(
                f"  {name:16s} {BOLD}{dpop:>3}{RST} pop   "
                f"Water {BBLU}{BOLD}{dw:>4}{RST}   Log {BOLD}{dl:>4}{RST}"
            ))

    if agent_data and isinstance(agent_data, dict):
        s = agent_data.get("status", "idle")
        if s != "idle":
            lines.append(hline())
            status_colors = {
                "gatheringstate": BYEL, "thinking": BMAG, "executing": BCYN,
                "done": BGRN, "error": BRED,
            }
            sc = status_colors.get(s, DIM)
            turn = agent_data.get("turn", 0)
            total = agent_data.get("totalTurns", 0)
            binary = agent_data.get("binary", "")
            model = agent_data.get("model", "")
            cur_cmd = agent_data.get("currentCmd", "")
            turn_bar = bar(turn, total, 16) if total > 0 else ""
            model_short = model.replace("claude-", "").replace("-20251001", "") if model else binary
            goal = agent_data.get("goal", "")
            lines.append(row(
                f"{BMAG}{BOLD}AGENT{RST}  {sc}{BOLD}{s}{RST}  turn {BOLD}{turn}{RST}/{total}  {turn_bar}",
                f"{DIM}{model_short}{RST}",
            ))
            if goal:
                lines.append(row(f"  {DIM}goal:{RST} {BOLD}{goal[:65]}{RST}"))

            if cur_cmd and s in ("gatheringstate", "thinking", "executing"):
                lines.append(row(f"  {BYEL}> {cur_cmd}{RST}"))

            history = agent_data.get("history", [])
            visible = history[-8:]
            for rec in visible:
                tn = rec.get("turn", 0)
                failed = rec.get("failed", 0)
                secs = rec.get("seconds", 0)
                cmds = rec.get("commands", [])
                err = rec.get("error", "")
                cmd_names: list[str] = []
                for c in cmds:
                    if c.startswith("ok: "):
                        cmd_names.append(f"{BCYN}{c[4:]}{RST}")
                    elif c.startswith("FAIL: "):
                        cmd_names.append(f"{BRED}{c[6:]}{RST}")
                    else:
                        cmd_names.append(c)
                summary_str = "  ".join(cmd_names[:4])
                extra = f" {DIM}+{len(cmd_names)-4}{RST}" if len(cmd_names) > 4 else ""
                fail_str = f" {BRED}{failed}fail{RST}" if failed else ""
                time_str = f"{secs:.0f}s" if secs >= 1 else "<1s"
                if err:
                    lines.append(row(f"  {DIM}t{tn}{RST} {DIM}{time_str:>4}{RST}  {RED}{err[:60]}{RST}"))
                else:
                    lines.append(row(
                        f"  {DIM}t{tn}{RST} {DIM}{time_str:>4}{RST}{fail_str}  {summary_str}{extra}"
                    ))

            err = agent_data.get("lastError", "")
            if err and s == "error":
                lines.append(row(f"  {RED}{err[:70]}{RST}"))

    lines.append(f" {DIM}{'─' * WIDTH}{RST}")
    agent_running = (
        agent_data
        and isinstance(agent_data, dict)
        and agent_data.get("status") not in ("idle", "done", "error", None)
    )
    if not agent_running:
        keys = f"[s]tart({agent_turns}t)  [+/-]turns  [0-3]speed  [q]uit"
    else:
        keys = "[x]stop  [0-3]speed  [q]uit"
    lines.append(f"  {DIM}{keys}  ·  refreshing every {interval}s{RST}")

    return "\n".join(lines)
