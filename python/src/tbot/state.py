"""Per-settlement persistent memory (brain.toon).

Replaces the module-level `_memory_dir` global from the legacy `timberbot.py`.
Each `SettlementContext` is bound to one settlement and one disk directory.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from tbot.paths import memory_base, sanitize_name


class SettlementContext:
    """Disk-backed memory for one settlement."""

    def __init__(self, settlement: str, base: Path | None = None) -> None:
        self.settlement = sanitize_name(settlement)
        self.base = base or memory_base()
        self.memory_dir = self.base / self.settlement

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
