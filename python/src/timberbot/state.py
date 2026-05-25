"""Per-settlement persistent memory (brain.toon).

Replaces the module-level `_memory_dir` global from the legacy `timberbot.py`.
Each `SettlementContext` is bound to one settlement and one disk directory.

Storage lives under the OS user-data dir (`config.data_dir() / "memory"`).
The pre-#43 location under the game's `Documents/Timberborn/Mods/Timberbot/`
tree is no longer consulted (PR 4 deleted the resolver). Users upgrading
through PR 3 had their `brain.toon` files auto-migrated; users skipping that
window can run `cp -r <old-mods>/Timberbot/memory/ <data-dir>/memory/`
manually.

This module also owns the `compact_summary` / `compact_locations` formatters
used to render brain output, since they're tightly coupled to the brain data
shape produced by `refresh_brain`.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from timberbot.config import data_dir

_FS_BAD = re.compile(r'[<>:"/\\|?*]')


def sanitize_name(name: str) -> str:
    """Make a settlement name filesystem-safe; never returns empty."""
    return _FS_BAD.sub("_", name).strip() or "unknown"


class SettlementContext:
    """Disk-backed memory for one settlement.

    The base directory is `config.data_dir() / "memory"` by default and can
    be overridden via the `base=` constructor arg (used by tests). Disk
    access is lazy so constructing a SettlementContext on a machine without
    a writable data dir doesn't raise.
    """

    def __init__(self, settlement: str, base: Path | None = None) -> None:
        self.settlement = sanitize_name(settlement)
        self._base_override = base

    @property
    def base(self) -> Path:
        if self._base_override is not None:
            return self._base_override
        return data_dir() / "memory"

    @property
    def memory_dir(self) -> Path:
        return self.base / self.settlement

    @property
    def brain_path(self) -> Path:
        return self.memory_dir / "brain.toon"

    def ensure_dir(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def load_brain(self) -> dict[str, Any]:
        """Load brain.toon or return an empty dict."""
        if not self.brain_path.exists():
            return {}
        try:
            import toons  # type: ignore[import-not-found]
            with open(self.brain_path) as f:
                return toons.load(f)
        except Exception:
            return {}

    def save_brain(self, brain: dict[str, Any]) -> None:
        """Persist brain.toon, creating the settlement dir if needed."""
        self.ensure_dir()
        import toons  # type: ignore[import-not-found]
        with open(self.brain_path, "w") as f:
            toons.dump(brain, f)

    def update_locations(self, locations: dict[str, Any]) -> None:
        brain = self.load_brain()
        brain["locations"] = locations
        self.save_brain(brain)

    def set_location(self, name: str, x: int, y: int, z: int = 0, note: str = "") -> dict[str, Any]:
        self.ensure_dir()
        brain = self.load_brain()
        locations = brain.get("locations", {})
        loc: dict[str, Any] = {"x": int(x), "y": int(y), "z": int(z)}
        if note:
            loc["note"] = note
        locations[name] = loc
        brain["locations"] = locations
        self.save_brain(brain)
        return {"saved": name, "x": loc["x"], "y": loc["y"], "z": loc["z"]}

    def remove_location(self, name: str) -> dict[str, Any]:
        self.ensure_dir()
        brain = self.load_brain()
        locations = brain.get("locations", {})
        if name not in locations:
            return {"error": "not_found", "name": name, "available": list(locations.keys())}
        del locations[name]
        brain["locations"] = locations
        self.save_brain(brain)
        return {"removed": name}

    def list_locations(self) -> dict[str, Any]:
        self.ensure_dir()
        return self.load_brain().get("locations", {})

    def add_task(self, action: str) -> dict[str, Any]:
        self.ensure_dir()
        brain = self.load_brain()
        tasks = brain.get("tasks", [])
        next_id = max((t["id"] for t in tasks), default=0) + 1
        task = {"id": next_id, "status": "pending", "action": action}
        tasks.append(task)
        brain["tasks"] = tasks
        self.save_brain(brain)
        return task

    def update_task(self, id: int, status: str, error: str | None = None) -> dict[str, Any]:
        self.ensure_dir()
        brain = self.load_brain()
        tasks = brain.get("tasks", [])
        for t in tasks:
            if t["id"] == id:
                t["status"] = status
                if error:
                    t["error"] = error
                elif "error" in t and status != "failed":
                    del t["error"]
                brain["tasks"] = tasks
                self.save_brain(brain)
                return t
        return {"error": f"task {id} not found"}

    def list_tasks(self) -> list[dict[str, Any]]:
        self.ensure_dir()
        return self.load_brain().get("tasks", [])

    def clear_tasks(self, status: str = "done") -> dict[str, Any]:
        self.ensure_dir()
        brain = self.load_brain()
        tasks = brain.get("tasks", [])
        before = len(tasks)
        brain["tasks"] = [t for t in tasks if t["status"] != status]
        self.save_brain(brain)
        return {"cleared": before - len(brain["tasks"]), "remaining": len(brain["tasks"])}

    # ------------------------------------------------------------------
    # Feedback (agent-reported bugs, inconsistencies, missing features)
    # ------------------------------------------------------------------

    @property
    def feedback_path(self) -> Path:
        return self.memory_dir / "feedback.toon"

    def _load_feedback(self) -> list[dict[str, Any]]:
        if not self.feedback_path.exists():
            return []
        try:
            import toons  # type: ignore[import-not-found]
            with open(self.feedback_path) as f:
                return toons.load(f) or []
        except Exception:
            return []

    def add_feedback(
        self, message: str, category: str = "bug", severity: str = "medium",
    ) -> dict[str, Any]:
        self.ensure_dir()
        import toons  # type: ignore[import-not-found]
        items = self._load_feedback()
        next_id = max((f["id"] for f in items), default=0) + 1
        item: dict[str, Any] = {
            "id": next_id,
            "timestamp": datetime.now().isoformat(),
            "category": category,
            "severity": severity,
            "message": message,
            "resolved": False,
        }
        items.append(item)
        with open(self.feedback_path, "w") as f:
            toons.dump(items, f)
        return item

    def list_feedback(self, resolved: bool = False) -> list[dict[str, Any]]:
        return [f for f in self._load_feedback() if f.get("resolved", False) == resolved]

    def resolve_feedback(self, id: int) -> dict[str, Any]:
        self.ensure_dir()
        import toons  # type: ignore[import-not-found]
        items = self._load_feedback()
        for item in items:
            if item["id"] == id:
                item["resolved"] = True
                with open(self.feedback_path, "w") as f:
                    toons.dump(items, f)
                return item
        return {"error": f"feedback {id} not found"}

    def clear(self) -> dict[str, Any]:
        if self.memory_dir.is_dir() and self.memory_dir != self.base:
            shutil.rmtree(self.memory_dir)
            return {"cleared": str(self.memory_dir)}
        return {"error": "no settlement memory to clear"}

    def refresh_brain(self, summary: dict[str, Any], goal: str | None = None) -> dict[str, Any]:
        """Update brain.toon with a new summary + goal, auto-seeding locations on first run.

        Mirrors the persistence + auto-seed logic of the legacy `Timberbot.brain()`.
        Caller passes the already-fetched `/api/summary` payload — this function
        does no HTTP. Returns the brain dict that was written to disk.
        """
        existing = self.load_brain()
        existing_goal = existing.get("goal", "")
        tasks = existing.get("tasks", [])
        locations = existing.get("locations", {})
        # migrate old `maps` key if present (legacy)
        if not locations and "maps" in existing:
            locations = {}

        current_goal = goal if goal else existing_goal

        # auto-seed locations from live data on first run
        if not locations:
            districts = summary.get("districts", [])
            dc = next((d.get("dc") for d in districts if d.get("dc")), None)
            if dc:
                locations["dc"] = {"x": dc["x"], "y": dc["y"], "z": dc.get("z", 0)}
            for i, tc in enumerate(summary.get("treeClusters", [])[:3]):
                label = "forest" if i == 0 else f"forest-{i+1}"
                locations[label] = {
                    "x": tc["x"], "y": tc["y"], "z": tc.get("z", 0),
                    "species": list(tc.get("species", {}).keys()),
                }
            for i, fc in enumerate(summary.get("foodClusters", [])[:3]):
                label = "berries" if i == 0 else f"berries-{i+1}"
                locations[label] = {
                    "x": fc["x"], "y": fc["y"], "z": fc.get("z", 0),
                    "species": list(fc.get("species", {}).keys()),
                }

        brain_data = {
            "timestamp": datetime.now().isoformat(),
            "goal": current_goal,
            "tasks": tasks,
            "locations": locations,
        }
        self.save_brain(brain_data)
        return brain_data


def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Mutate-and-return a compact view of `/api/summary` for brain output.

    Flattens nested time/weather/district/wellbeing/cluster shapes into the
    one-line CSV-style format the AI agent consumes via brain.toon. Mutates
    the input dict in place (and returns it) to avoid copying.
    """
    s = summary
    if "time" in s:
        t = s.pop("time")
        insert: dict[str, Any] = {}
        for k in list(s.keys()):
            insert[k] = s.pop(k)
            if k == "faction":
                insert["day"] = t.get("dayNumber", 0)
                insert["dayProgress"] = round(t.get("dayProgress", 0), 2)
                insert["speed"] = t.get("speed", 0)
        s.update(insert)
    if "weather" in s:
        w = s.pop("weather")
        insert = {}
        for k in list(s.keys()):
            insert[k] = s.pop(k)
            if k == "speed":
                insert["weather"] = (
                    f'cycle {w.get("cycle",0)} day {w.get("cycleDay",0)} '
                    f'{"DROUGHT" if w.get("isHazardous") else "temperate"} '
                    f'{w.get("temperateWeatherDuration",0)}t/{w.get("hazardousWeatherDuration",0)}d'
                )
        s.update(insert)
    if "districts" in s:
        compact_districts: list[dict[str, Any]] = []
        for d in s["districts"]:
            cd: dict[str, Any] = {"name": d.get("name", "")}
            pop = d.get("population", {})
            cd["pop"] = f'{pop.get("adults",0)}a {pop.get("children",0)}c {pop.get("bots",0)}b'
            res = d.get("resources", {})
            cd["resources"] = " ".join(f"{k}:{v}" for k, v in res.items())
            h = d.get("housing", {})
            cd["beds"] = f'{h.get("occupiedBeds",0)}/{h.get("totalBeds",0)} homeless:{h.get("homeless",0)}'
            e = d.get("employment", {})
            cd["workers"] = f'{e.get("assigned",0)}/{e.get("vacancies",0)} idle:{e.get("unemployed",0)}'
            wb = d.get("wellbeing", {})
            cd["wellbeing"] = f'{wb.get("average",0)}/77 miserable:{wb.get("miserable",0)} critical:{wb.get("critical",0)}'
            dc = d.get("dc", {})
            if dc:
                cd["dc"] = (
                    f'{dc["x"]},{dc["y"]},z{dc.get("z",0)} {dc.get("orientation","")} '
                    f'entrance:{dc.get("entranceX",0)},{dc.get("entranceY",0)}'
                )
            compact_districts.append(cd)
        s["districts"] = compact_districts
    if "trees" in s and isinstance(s["trees"], dict):
        sp = s["trees"].get("species", [])
        s["trees"] = {
            "marked": s["trees"].get("markedGrown", 0),
            "seedling": s["trees"].get("markedSeedling", 0),
            "unmarked": s["trees"].get("unmarkedGrown", 0),
            "species": [dict(x) for x in sp],
        }
    if "crops" in s and isinstance(s["crops"], dict):
        sp = s["crops"].get("species", [])
        s["crops"] = {
            "ready": s["crops"].get("ready", 0),
            "growing": s["crops"].get("growing", 0),
            "species": [dict(x) for x in sp],
        }
    if "wellbeing" in s and isinstance(s["wellbeing"], dict):
        cats = s["wellbeing"].get("categories", [])
        s["wellbeing"] = {
            "avg": s["wellbeing"].get("average", 0),
            "miserable": s["wellbeing"].get("miserable", 0),
            "critical": s["wellbeing"].get("critical", 0),
            "categories": [dict(c) for c in cats],
        }
    for key in ("treeClusters", "foodClusters"):
        if key in s:
            s[key] = [
                {
                    "x": c["x"], "y": c["y"], "z": c.get("z", 0),
                    "grown": c.get("grown", 0), "total": c.get("total", 0),
                    "species": ",".join(c.get("species", {}).keys()),
                }
                for c in s[key]
            ]
    return s


def compact_locations(locations: dict[str, Any]) -> dict[str, str]:
    """Render the brain.toon `locations` dict as compact one-line strings."""
    out: dict[str, str] = {}
    for name, loc in locations.items():
        sp = ",".join(loc.get("species", [])) if "species" in loc else ""
        note = loc.get("note", "")
        val = f'{loc["x"]},{loc["y"]},z{loc.get("z",0)}'
        if sp:
            val += " " + sp
        if note:
            val += " " + note
        out[name] = val
    return out
