#!/usr/bin/env python
"""Timberbot. control Timberborn over HTTP.

CLI for the Timberbot API (port 8085). Talks to the C# mod running inside the game.
The API does all data processing; this client is a thin wrapper that formats output.

Output formats:
    TOON (default): compact tabular format optimized for AI token efficiency
    JSON (--json):  full nested data for programmatic access

Usage:
    timberbot.py                     list all methods
    timberbot.py summary             colony dashboard (one call, all stats)
    timberbot.py buildings           list all buildings
    timberbot.py --json summary      full JSON output
    timberbot.py top                 live colony dashboard
    timberbot.py place_building prefab:LumberjackFlag.IronTeeth x:120 y:130 z:2

As a library:
    from timberbot import Timberbot
    bot = Timberbot()                       # toon format (flat)
    bot = Timberbot(json_mode=True)         # json format (full)
    bot.summary()
"""
import json
import os
import platform
import re
import subprocess
import sys
import time
from typing import Any, cast

import requests


def _timberborn_documents_dir():
    return os.path.join(os.path.expanduser("~"), "Documents", "Timberborn")


def _mod_dir():
    return os.path.join(_timberborn_documents_dir(), "Mods", "Timberbot")


def _settings_path():
    return os.path.join(_mod_dir(), "settings.json")


def _saves_dir():
    return os.path.join(_timberborn_documents_dir(), "Saves")


_MEMORY_BASE = os.path.join(_mod_dir(), "memory")
_memory_dir = _MEMORY_BASE  # overridden per-settlement by brain()


def _sanitize_name(name: str) -> str:
    """Sanitize settlement name for filesystem."""
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip() or "unknown"


def _load_brain_file(mdir: str | None = None) -> dict[str, Any]:
    """Load brain.toon or return empty dict."""
    d = mdir or _memory_dir
    bpath = os.path.join(d, "brain.toon")
    if os.path.exists(bpath):
        try:
            import toons  # pyright: ignore[reportMissingImports]
            with open(bpath) as f:
                return toons.load(f)
        except Exception:
            pass
    return {}


def _save_brain_file(brain: dict[str, Any], mdir: str | None = None) -> None:
    """Write brain.toon."""
    d = mdir or _memory_dir
    os.makedirs(d, exist_ok=True)
    import toons  # pyright: ignore[reportMissingImports]
    with open(os.path.join(d, "brain.toon"), "w") as f:
        toons.dump(brain, f)


def _update_brain_locations(locations: dict[str, Any], mdir: str | None = None) -> None:
    """Update the locations dict in brain.toon."""
    brain = _load_brain_file(mdir)
    brain["locations"] = locations
    _save_brain_file(brain, mdir)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class TimberbotError(Exception):
    """API returned an error response. e.code is the prefix before ':', e.response is the full dict."""
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.error = response.get("error", "unknown")
        self.code = self.error.split(":")[0].strip()
        super().__init__(self.error)


class Timberbot:
    """Client for Timberbot API (port 8085).

    All data processing happens server-side in the C# mod. This client sends
    a format param ("toon" or "json") and passes the response straight through.
    No client-side transformation of API data.
    """

    def __init__(self, host: str | None = None, port: int | None = None, json_mode: bool = False, write_timeout: int = 60) -> None:
        if host is None or port is None:
            try:
                with open(_settings_path()) as f:
                    settings = json.load(f)
                if host is None:
                    host = settings.get("httpHost", "127.0.0.1")
                if port is None:
                    port = settings.get("httpPort", 8085)
            except Exception:
                host = host or "127.0.0.1"
                port = port or 8085
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        self._format = "json" if json_mode else "toon"
        self._write_timeout = write_timeout
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"

    def _check(self, data: Any) -> Any:
        if isinstance(data, dict) and "error" in data:
            raise TimberbotError(data)
        return data

    def _get(self, path: str, params: dict[str, int | str] | None = None) -> dict[str, Any]:
        p: dict[str, int | str] = {"format": self._format}
        if params:
            p.update(params)
        r = self.s.get(f"{self.url}{path}", params=p, timeout=5)
        r.raise_for_status()
        return self._check(r.json())

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        data["format"] = self._format
        r = self.s.post(f"{self.url}{path}", json=data, timeout=self._write_timeout)
        return self._check(r.json())

    def _post_json(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """Force JSON format for internal programmatic use."""
        data["format"] = "json"
        r = self.s.post(f"{self.url}{path}", json=data, timeout=self._write_timeout)
        return self._check(r.json())

    def _get_json(self, path: str, params: dict[str, int | str] | None = None) -> dict[str, Any]:
        """Force JSON format for internal programmatic use."""
        p: dict[str, int | str] = {"format": "json"}
        if params:
            p.update(params)
        r = self.s.get(f"{self.url}{path}", params=p, timeout=5)
        r.raise_for_status()
        return self._check(r.json())

    #. connection --

    def ping(self) -> bool:
        """True if Timberbot mod is reachable."""
        try:
            return self._get("/api/ping").get("ready", False)
        except (requests.ConnectionError, requests.Timeout):
            return False

    #. webhooks --

    def register_webhook(self, url: str, events: list[str] | None = None) -> dict[str, Any]:
        """Register a webhook URL to receive push notifications for game events.
        events: list of event names to subscribe to (None = all events).
        Available: drought.start, drought.end, building.placed, building.demolished,
                   beaver.born, beaver.died, day.start, night.start"""
        data: dict[str, Any] = {"url": url}
        if events:
            data["events"] = events
        return self._post("/api/webhooks", data)

    def unregister_webhook(self, id: int) -> dict[str, Any]:
        """Unregister a webhook by ID."""
        return self._post("/api/webhooks/delete", {"id": id})

    def list_webhooks(self) -> dict[str, Any]:
        """List all registered webhooks."""
        return self._get("/api/webhooks")

    #. read state (nouns) --

    def summary(self) -> dict[str, Any]:
        """Full snapshot: time + weather + districts with resources and population."""
        return self._get("/api/summary")

    def time(self) -> dict[str, Any]:
        """Game time: {dayNumber, dayProgress, partialDayNumber}."""
        return self._get("/api/time")

    def weather(self) -> dict[str, Any]:
        """Weather: {cycle, cycleDay, isHazardous, temperateWeatherDuration, hazardousWeatherDuration}."""
        return self._get("/api/weather")

    def population(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Beaver counts: [{district, adults, children, bots}]."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/population"))

    def resources(self) -> dict[str, Any]:
        """Resource stocks: {districtName: {goodName: {available, all}}}."""
        return self._get("/api/resources")

    def districts(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Districts: [{name, population: {adults, children, bots}, resources: {...}}]."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/districts"))

    def buildings(self, limit: int = 0, offset: int = 0, detail: str = "basic", id: int = 0, name: str = "", x: int = 0, y: int = 0, radius: int = 0) -> list[dict[str, Any]] | dict[str, Any]:
        """All buildings. detail: basic|full. id: single building. name: substring filter. x/y/radius: proximity filter."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if id:
            params["id"] = id
        if detail != "basic":
            params["detail"] = detail
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/buildings", params=params))

    def buildings_v2(self, limit: int = 0, offset: int = 0, detail: str = "basic", id: int = 0, name: str = "", x: int = 0, y: int = 0, radius: int = 0) -> list[dict[str, Any]] | dict[str, Any]:
        """Compatibility alias for buildings()."""
        return self.buildings(limit=limit, offset=offset, detail=detail, id=id, name=name, x=x, y=y, radius=radius)

    def trees(self, limit: int = 0, offset: int = 0, name: str = "", x: int = 0, y: int = 0, radius: int = 0) -> list[dict[str, Any]] | dict[str, Any]:
        """Trees: [{id, name, x, y, z, marked, alive, grown, growth}]. name: species filter. x/y/radius: proximity."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/trees", params=params))

    def crops(self, limit: int = 0, offset: int = 0, name: str = "", x: int = 0, y: int = 0, radius: int = 0) -> list[dict[str, Any]] | dict[str, Any]:
        """Crops in the ground: [{id, name, x, y, z, marked, alive, grown, growth}]. name: crop filter. x/y/radius: proximity."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/crops", params=params))

    def gatherables(self, limit: int = 0, offset: int = 0, name: str = "", x: int = 0, y: int = 0, radius: int = 0) -> list[dict[str, Any]] | dict[str, Any]:
        """All gatherable resources (berry bushes etc): [{id, name, x, y, z, alive}]. name/x/y/radius: filters."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/gatherables", params=params))

    def beavers(self, limit: int = 0, offset: int = 0, detail: str = "basic", id: int = 0, name: str = "", x: int = 0, y: int = 0, radius: int = 0) -> list[dict[str, Any]] | dict[str, Any]:
        """All beavers with wellbeing and needs. detail:full for all needs. name/x/y/radius: filters."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if id:
            params["id"] = id
        if detail != "basic":
            params["detail"] = detail
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/beavers", params=params))

    def workhours(self) -> dict[str, Any]:
        """Work schedule: {endHours, areWorkingHours, hoursPassedToday}."""
        return self._get("/api/workhours")

    def migrate(self, from_district: str, to_district: str, count: int = 1) -> dict[str, Any]:
        """Move beavers between districts."""
        return self._post("/api/district/migrate", {
            "from": from_district, "to": to_district, "count": count
        })

    def set_workhours(self, end_hours: int) -> dict[str, Any]:
        """Set when work ends (1-24). Beavers work from dawn until endHours."""
        return self._post("/api/workhours", {"endHours": end_hours})

    def science(self) -> dict[str, Any]:
        """Science points and unlockable buildings: {points, unlockables: [{name, cost, unlocked}]}."""
        return self._get("/api/science")

    def wellbeing(self) -> dict[str, Any]:
        """Population wellbeing breakdown by category: {beavers, categories: [{group, current, max, needs}]}."""
        return self._get("/api/wellbeing")

    def unlock_building(self, building: str) -> dict[str, Any]:
        """Unlock a building using science points."""
        return self._post("/api/science/unlock", {"building": building})

    def notifications(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Game notification history: [{subject, description, entityId, cycle, cycleDay}]."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/notifications"))

    def alerts(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Alerts: unstaffed, unpowered, unreachable, status issues."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/alerts"))

    def distribution(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Distribution settings per district: [{district, goods: [{good, importOption, exportThreshold}]}]."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/distribution"))

    def set_distribution(self, district: str, good: str, import_option: str = "", export_threshold: int = -1) -> dict[str, Any]:
        """Set import/export for a good in a district. import_option: Forced, Auto, None."""
        return self._post("/api/distribution", {
            "district": district, "good": good,
            "import": import_option, "exportThreshold": export_threshold
        })

    def prefabs(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Available building templates: [{name, sizeX, sizeY, sizeZ}]."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/prefabs"))

    def power(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Power networks: [{id, supply, demand, buildings}]."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/power"))

    def speed(self) -> dict[str, Any]:
        """Current game speed: {speed: 0-3}."""
        return self._get("/api/speed")

    def tiles(self, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0) -> dict[str, Any]:
        """Tile data for a region: terrain, water, occupants, moisture, contamination. No args = map size only."""
        return self._get("/api/tiles", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    #. write actions (verb_noun) --

    def set_speed(self, speed: int) -> dict[str, Any]:
        """Set game speed. 0=pause, 1=normal, 2=fast, 3=fastest."""
        return self._post("/api/speed", {"speed": speed})

    def pause_building(self, id: int) -> dict[str, Any]:
        """Pause a building."""
        return self._post("/api/building/pause", {"id": id, "paused": True})

    def unpause_building(self, id: int) -> dict[str, Any]:
        """Unpause a building."""
        return self._post("/api/building/pause", {"id": id, "paused": False})

    def set_clutch(self, id: int, engaged: bool) -> dict[str, Any]:
        """Engage or disengage a clutch. engaged: True/False."""
        return self._post("/api/building/clutch", {"id": id, "engaged": engaged})

    def set_priority(self, id: int, priority: str, type: str = "") -> dict[str, Any]:
        """Set building priority. Values: VeryLow, Normal, VeryHigh. Type: workplace (finished) or construction (building)."""
        return self._post("/api/building/priority", {"id": id, "priority": priority, "type": type})

    def set_haul_priority(self, id: int, prioritized: bool = True) -> dict[str, Any]:
        """Set hauler priority on a building. Haulers will deliver goods here first."""
        return self._post("/api/building/hauling", {"id": id, "prioritized": prioritized})

    def set_recipe(self, id: int, recipe: str) -> dict[str, Any]:
        """Set manufactory recipe. Use 'none' to clear. Lists available recipes on error."""
        return self._post("/api/building/recipe", {"id": id, "recipe": recipe})

    def set_farmhouse_action(self, id: int, action: str) -> dict[str, Any]:
        """Set farmhouse priority action: 'planting' or 'harvesting'."""
        return self._post("/api/building/farmhouse", {"id": id, "action": action})

    def set_plantable_priority(self, id: int, plantable: str) -> dict[str, Any]:
        """Set prioritized plantable on forester/gatherer. Use 'none' to clear."""
        return self._post("/api/building/plantable", {"id": id, "plantable": plantable})

    def set_workers(self, id: int, count: int) -> dict[str, Any]:
        """Set desired worker count (0 to maxWorkers)."""
        return self._post("/api/building/workers", {"id": id, "count": count})

    def set_floodgate(self, id: int, height: float) -> dict[str, Any]:
        """Set floodgate height (clamped to min/max)."""
        return self._post("/api/building/floodgate", {"id": id, "height": height})

    def debug(self, target: str = "help", **kwargs: Any) -> dict[str, Any]:
        """Generic live debug surface. Targets include help, roots, get, fields, describe, call, compare, assert, validate, validate_all."""
        body = {"target": target}
        body.update(kwargs)
        return self._post("/api/debug", body)

    def find_placement(self, prefab: str, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, x: int | None = None, y: int | None = None, radius: int | None = None) -> dict[str, Any]:
        """Find valid placements for a building in an area. Returns spots sorted by path access."""
        body = {"prefab": prefab, "x1": x1, "y1": y1, "x2": x2, "y2": y2}
        if x is not None and y is not None:
            body["x"] = x
            body["y"] = y
            if radius is not None: body["radius"] = radius
        return self._post("/api/placement/find", body)

    def place_building(self, prefab: str, x: int, y: int, z: int, orientation: str = "south") -> dict[str, Any]:
        """Place a building. Orientation: south, west, north, east."""
        return self._post("/api/building/place", {
            "prefab": prefab, "x": x, "y": y, "z": z,
            "orientation": str(orientation).lower()
        })

    def demolish_building(self, id: int) -> dict[str, Any]:
        """Demolish a building. Get IDs from buildings()."""
        return self._post("/api/building/demolish", {"id": id})

    def demolish_crop(self, id: int) -> dict[str, Any]:
        """Demolish a planted crop entity by ID. Get IDs from crops()."""
        return self._post("/api/crop/demolish", {"id": id})

    def mark_trees(self, x1: int, y1: int, x2: int, y2: int, z: int) -> dict[str, Any]:
        """Mark a rectangular area for tree cutting."""
        return self._post("/api/cutting/area", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z, "marked": True
        })

    def plant_crop(self, x1: int, y1: int, x2: int, y2: int, z: int, crop: str) -> dict[str, Any]:
        """Mark area for planting. Crops: Kohlrabi, Cassava, Carrot, Potato, Wheat, etc."""
        return self._post("/api/planting/mark", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z, "crop": crop
        })

    def find_planting(self, crop: str, id: int = 0, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, z: int = 0) -> dict[str, Any]:
        """Find valid planting spots. Use id for farmhouse range, or x1/y1/x2/y2/z for area."""
        return self._post("/api/planting/find", {
            "crop": crop, "id": id,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z
        })

    def building_range(self, id: int) -> dict[str, Any]:
        """Get work range tiles for a building (farmhouse, lumberjack, forester)."""
        return self._post("/api/building/range", {"id": id})

    def clear_planting(self, x1: int, y1: int, x2: int, y2: int, z: int) -> dict[str, Any]:
        """Clear planting marks from a rectangular area."""
        return self._post("/api/planting/clear", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z
        })

    def clear_trees(self, x1: int, y1: int, x2: int, y2: int, z: int) -> dict[str, Any]:
        """Clear tree cutting marks from a rectangular area."""
        return self._post("/api/cutting/area", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z, "marked": False
        })

    def set_storage(self, id: int, good: str = "", mode: str = "") -> dict[str, Any]:
        """Set storage mode and/or allowed good. mode: accept, obtain, supply, empty. good: good name or 'none' to clear."""
        body: dict[str, int | str] = {"id": id}
        if good: body["good"] = good
        if mode: body["mode"] = mode
        return self._post("/api/building/storage", body)

    def place_path(self, x1: int, y1: int, x2: int, y2: int, _z: int = 0, style: str = "direct", sections: int = 0, timings: bool = False) -> dict[str, Any]:
        """Route a path using A* to avoid obstacles, with auto-stairs at z-level changes. z param ignored. style: 'direct' (staircase) or 'straight' (minimize turns). sections: 0=all, N=place N stair crossings then stop."""
        body: dict[str, Any] = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "style": style}
        if sections: body["sections"] = sections
        if timings: body["timings"] = True
        return self._post("/api/path/place", body)

    #. automation --

    def link(self, source_id: int, target_id: int, input: str = "a"):
        """Wire a sensor/relay output to a building automation input. input: a, b, or reset (for Memory)."""
        return self._post("/api/automation/link", {"sourceId": source_id, "targetId": target_id, "input": input})

    def unlink(self, id: int, input: str = "a"):
        """Disconnect an automation input. input: a, b, or reset (for Memory)."""
        return self._post("/api/automation/unlink", {"id": id, "input": input})

    def configure_automation(self, id: int, property: str, value: str):
        """Configure an automation component property (threshold, mode, etc.)."""
        return self._post("/api/automation/configure", {"id": id, "property": property, "value": value})

    def rename_automation(self, id: int, name: str):
        """Set a custom label for an automation entity."""
        return self._post("/api/automation/rename", {"id": id, "name": name})

    #. helpers --

    def tree_clusters(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Find clusters of grown trees. Returns top clusters by grown count."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/tree_clusters"))

    def food_clusters(self) -> list[dict[str, Any]] | dict[str, Any]:
        """Find clusters of gatherable food (berries, bushes). Returns top clusters by grown count."""
        return cast(list[dict[str, Any]] | dict[str, Any], self._get("/api/food_clusters"))

    @staticmethod
    def near(items: list[dict[str, Any]], x: int, y: int, radius: int = 20) -> list[dict[str, Any]]:
        """Filter items to those within radius of (x,y). Sorted by distance."""
        result = []
        for i in items:
            if "x" not in i:
                continue
            d = abs(i["x"] - x) + abs(i["y"] - y)
            if d <= radius:
                result.append(i)
        result.sort(key=lambda i: abs(i["x"] - x) + abs(i["y"] - y))
        return result

    @staticmethod
    def named(items: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
        """Filter items whose name contains the given string (case-insensitive)."""
        low = name.lower()
        return [i for i in items if low in i.get("name", "").lower()]

    def map(self, x1: int, y1: int, x2: int, y2: int) -> dict[str, Any]:
        """Colored ASCII map with terrain height shading, buildings, water, trees."""
        R = "\033[0m"
        DIM = "\033[2m"
        RED = "\033[31m"
        GRN = "\033[32m"
        YEL = "\033[33m"
        BLU = "\033[34m"
        MAG = "\033[35m"
        CYN = "\033[36m"
        BGRN = "\033[92m"
        BYEL = "\033[93m"
        BBLU = "\033[94m"
        BMAG = "\033[95m"
        BWHT = "\033[97m"
        BOLD = "\033[1m"

        STYLE = {
            "Path": ("=", YEL),
            "DistrictCenter": ("D", BOLD + BYEL),
            "Rowhouse": ("H", YEL), "Barrack": ("H", YEL), "Lodge": ("H", YEL),
            "Breeding": ("R", YEL),
            "LumberMill": ("M", BWHT), "WoodWorkshop": ("M", BWHT),
            "IndustrialLumberMill": ("M", BWHT),
            "FarmHouse": ("F", CYN), "Forester": ("f", GRN),
            "PowerWheel": ("E", BYEL), "PowerShaft": ("E", BYEL),
            "Inventor": ("S", BWHT), "Numbercruncher": ("S", BWHT),
            "Lumberjack": ("L", RED), "Gatherer": ("G", MAG),
            "Hauling": ("K", RED), "Scavenger": ("G", RED),
            "Pump": ("P", BBLU), "Tank": ("W", BBLU),
            "Floodgate": ("X", CYN), "Dam": ("X", CYN),
            "Levee": ("X", CYN), "Sluice": ("X", CYN),
            "Warehouse": ("$", YEL), "Pile": ("$", YEL),
            "Pine": ("T", GRN), "Birch": ("T", GRN), "Oak": ("T", GRN),
            "Maple": ("T", GRN), "Chestnut": ("T", GRN),
            "Bush": ("B", MAG), "berry": ("B", MAG),
            "Kohlrabi": ("k", BGRN), "Carrot": ("c", BGRN),
            "Potato": ("p", BGRN), "Wheat": ("w", BGRN),
            "Cassava": ("a", BGRN), "Sunflower": ("s", BGRN),
            "Corn": ("n", BGRN), "Eggplant": ("e", BGRN),
            "Cattail": ("l", BGRN), "Spadderdock": ("d", BGRN),
            "Soybean": ("y", BGRN), "Canola": ("o", BGRN),
            "Campfire": ("C", RED),
            "Stairs": ("/", YEL), "Platform": ("_", YEL),
            "Metalsmith": ("m", BWHT), "Smelter": ("m", BWHT),
            "GearWorkshop": ("g", BWHT),
            "BotAssembler": ("b", BMAG), "BotPartFactory": ("b", BMAG),
            "ChargingStation": ("z", BMAG),
            "FluidDump": ("V", BBLU), "DoubleShower": ("v", BBLU),
            "SwimmingPool": ("v", BBLU),
            "Scratcher": ("~", GRN), "Bench": ("~", GRN),
            "ExercisePlaza": ("~", GRN), "MedicalBed": ("~", GRN),
            "Brazier": ("*", RED), "Lantern": ("*", YEL),
            "BeaverBust": ("*", YEL), "Roof": ("^", DIM),
            "Ruin": ("R", DIM), "Relic": ("R", DIM),
            "FoodFactory": ("F", CYN),
            "Slope": ("/", DIM),
            "AncientAquiferDrill": ("A", BBLU),
            "Shrub": ("B", MAG), "Geothermal": ("G", RED),
            # water
            "CompactWaterWheel": ("P", BBLU), "LargeWaterWheel": ("P", BBLU),
            "BadwaterDischarge": ("V", BBLU), "Centrifuge": ("V", BBLU),
            "Valve": ("X", CYN), "FillValve": ("X", CYN),
            "AquiferDrill": ("A", BBLU), "IrrigationBarrier": ("X", CYN),
            # power
            "SteamEngine": ("E", BYEL), "GravityBattery": ("E", BYEL),
            "Clutch": ("E", BYEL),
            # production
            "CoffeeBrewery": ("F", CYN), "OilPress": ("F", CYN),
            "Fermenter": ("F", CYN), "TappersShack": ("F", CYN),
            "ExplosivesFactory": ("F", CYN), "HydroponicGarden": ("F", CYN),
            "EfficientMine": ("F", CYN), "GreaseFactory": ("F", CYN),
            "WoodWorkshop": ("M", BWHT),
            # amenities
            "Detailer": ("~", GRN), "MudBath": ("~", GRN),
            "WindTunnel": ("~", GRN), "Motivatorium": ("~", GRN),
            "TeethGrindstone": ("~", GRN), "DecontaminationPod": ("~", GRN),
            # decorations
            "BeaverStatue": ("*", YEL), "Bell": ("*", YEL),
            "DecorativeClock": ("*", YEL), "MetalFence": ("|", DIM),
            "WoodFence": ("|", DIM), "PoleBanner": ("!", YEL),
            "SquareBanner": ("!", YEL), "FireworkLauncher": ("!", YEL),
            "StreamGauge": ("*", DIM),
            # infrastructure
            "Gate": ("=", YEL), "Tunnel": ("=", YEL),
            "DistrictCrossing": ("=", YEL),
            "Tubeway": ("=", BMAG), "TubewayStation": ("=", BMAG),
            "VerticalTubeway": ("=", BMAG),
            "SuspensionBridge": ("=", YEL), "Overhang": ("_", DIM),
            "ImpermeableFloor": ("_", DIM), "TerrainBlock": ("#", DIM),
            "DirtExcavator": ("#", DIM),
            # automation
            "Lever": ("i", DIM), "Sensor": ("i", DIM), "Timer": ("i", DIM),
            "Memory": ("i", DIM), "Relay": ("i", DIM), "Indicator": ("i", DIM),
            "Speaker": ("i", DIM), "HttpAdapter": ("i", DIM), "HttpLever": ("i", DIM),
            "Chronometer": ("i", DIM), "Counter": ("i", DIM),
            "WeatherStation": ("i", DIM), "PowerMeter": ("i", DIM),
            # wonders
            "LaborerMonument": ("Q", BYEL), "FlameOfUnity": ("Q", BYEL),
            "TributeToIngenuity": ("Q", BYEL), "EarthRepopulator": ("Q", BYEL),
            # explosives
            "Dynamite": ("x", RED), "DoubleDynamite": ("x", RED),
            "TripleDynamite": ("x", RED), "Detonator": ("x", RED),
            # misc
            "BuildersHut": ("K", RED), "ControlTower": ("b", BMAG),
            "Numbercruncher": ("S", BWHT),
        }

        def _zbg(z: int) -> str:
            # gradient within tens bands: 0-9 dark(234-242), 10-19 bright(244-252), 20-22 brightest(254+)
            if z < 10:
                shade = 234 + z
            elif z < 20:
                shade = 244 + (z - 10)
            else:
                shade = 254 + min(z - 20, 1)
            return f"\033[48;5;{min(shade, 255)}m"

        data = self._get_json("/api/tiles", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})
        tiles = {(t["x"], t["y"]): t for t in data.get("tiles", [])}
        legend = {}
        z_levels = set()

        lines = []
        for ty in range(y2, y1 - 1, -1):
            row = f"{DIM}{ty:3d}{R} "
            pbg = pco = ""
            for tx in range(x1, x2 + 1):
                t = tiles.get((tx, ty))
                if not t:
                    if pbg or pco:
                        row += R
                        pbg = pco = ""
                    row += f"{DIM}?{R}"
                    continue
                occ = t.get("occupants")
                occupant = max(occ, key=lambda o: o["z"])["name"] if occ else None
                entrance = t.get("entrance", False)
                bg = co = ch = None
                if entrance and not occupant:
                    bg = _zbg(t["terrain"])
                    z_levels.add(t["terrain"])
                    co = BWHT
                    ch = "@"
                    legend["@"] = (BWHT, "entrance")
                elif occupant:
                    oname = occupant
                    bg = _zbg(t["terrain"])
                    z_levels.add(t["terrain"])
                    for key, (c, s) in STYLE.items():
                        if key.lower() in oname.lower():
                            ch, co = c, s
                            legend[c] = (s, key)
                            break
                    if ch == "T" and t.get("seedling"):
                        ch, co = "t", DIM + GRN
                        legend["t"] = (co, "seedling")
                    if not ch:
                        ch = oname[0]
                        co = DIM
                        legend[ch] = (DIM, oname)
                elif t.get("water", 0) > 0:
                    bg = _zbg(t["terrain"])
                    z_levels.add(t["terrain"])
                    co = BLU
                    ch = "~"
                    legend["~"] = (BLU, "water")
                elif t["terrain"] > 0:
                    bg = _zbg(t["terrain"])
                    z_levels.add(t["terrain"])
                    ch = str(t["terrain"] % 10)
                    co = GRN if t.get("moist") else DIM
                else:
                    if pbg or pco:
                        row += R
                        pbg = pco = ""
                    row += " "
                    continue
                delta = ""
                if bg != pbg:
                    delta += bg or ""
                if co != pco:
                    delta += co or ""
                row += delta + ch
                pbg = bg
                pco = co
            if pbg or pco:
                row += R
            lines.append(row)

        axis = f"    {DIM}" + "".join(str(i % 10) for i in range(x1, x2 + 1)) + R
        lines.append(axis)

        leg = "  "
        for ch, (co, label) in sorted(legend.items(), key=lambda x: x[1][1]):
            leg += f" {co}{ch}{R} {label}"
        lines.append(leg)

        if len(z_levels) > 1:
            zleg = "   height:"
            for z in sorted(z_levels):
                zleg += f" {_zbg(z)} z={z} {R}"
            lines.append(zleg)

        # print directly to terminal instead of returning as JSON
        print("\n".join(lines))
        return {"rendered": True, "tiles": len(tiles)}

    # ------------------------------------------------------------------
    # Spatial memory
    # ------------------------------------------------------------------

    def brain(self, goal: str | None = None) -> dict[str, Any]:
        """Live summary + persistent goal/tasks/locations."""
        global _memory_dir

        summary = self._get_json("/api/summary")

        # set per-settlement memory dir
        settlement = _sanitize_name(summary.get("settlement", summary.get("settlementName", "unknown")))
        _memory_dir = os.path.join(_MEMORY_BASE, settlement)

        # load persistent data
        existing_goal = ""
        tasks = []
        locations = {}
        bpath = os.path.join(_memory_dir, "brain.toon")
        if os.path.exists(bpath):
            try:
                import toons as _t  # pyright: ignore[reportMissingImports]
                with open(bpath) as f:
                    old = _t.load(f)
                    existing_goal = old.get("goal", "")
                    tasks = old.get("tasks", [])
                    locations = old.get("locations", {})
                    # migrate old maps key if present
                    if not locations and "maps" in old:
                        locations = {}
            except Exception:
                pass

        # goal: new param overwrites, otherwise keep existing
        current_goal = goal if goal else existing_goal

        # auto-seed locations from live data on first run
        if not locations:
            districts = summary.get("districts", [])
            dc = next((d.get("dc") for d in districts if d.get("dc")), None)
            if dc:
                locations["dc"] = {"x": dc["x"], "y": dc["y"], "z": dc.get("z", 0)}
            tree_clusters = summary.get("treeClusters", [])
            for i, tc in enumerate(tree_clusters[:3]):
                label = "forest" if i == 0 else f"forest-{i+1}"
                locations[label] = {"x": tc["x"], "y": tc["y"], "z": tc.get("z", 0), "species": list(tc.get("species", {}).keys())}
            food_clusters = summary.get("foodClusters", [])
            for i, fc in enumerate(food_clusters[:3]):
                label = "berries" if i == 0 else f"berries-{i+1}"
                locations[label] = {"x": fc["x"], "y": fc["y"], "z": fc.get("z", 0), "species": list(fc.get("species", {}).keys())}

        # persist brain.toon
        import toons as _t  # pyright: ignore[reportMissingImports]
        os.makedirs(_memory_dir, exist_ok=True)
        from datetime import datetime
        brain_data = {"timestamp": datetime.now().isoformat(), "goal": current_goal, "tasks": tasks, "locations": locations}
        with open(bpath, "w") as f:
            _t.dump(brain_data, f)

        # compact summary: flatten nested dicts into CSV-style for toon rendering
        s = summary
        # flatten time (insert after faction to keep at top)
        if "time" in s:
            t = s.pop("time")
            # insert after settlement/faction
            insert = {}
            for k in list(s.keys()):
                insert[k] = s.pop(k)
                if k == "faction":
                    insert["day"] = t.get("dayNumber", 0)
                    insert["dayProgress"] = round(t.get("dayProgress", 0), 2)
                    insert["speed"] = t.get("speed", 0)
            s.update(insert)
        # flatten weather (insert right after speed)
        if "weather" in s:
            w = s.pop("weather")
            insert = {}
            for k in list(s.keys()):
                insert[k] = s.pop(k)
                if k == "speed":
                    insert["weather"] = f'cycle {w.get("cycle",0)} day {w.get("cycleDay",0)} {"DROUGHT" if w.get("isHazardous") else "temperate"} {w.get("temperateWeatherDuration",0)}t/{w.get("hazardousWeatherDuration",0)}d'
            s.update(insert)
        # flatten districts into compact rows
        if "districts" in s:
            compact_districts = []
            for d in s["districts"]:
                cd = {"name": d.get("name", "")}
                pop = d.get("population", {})
                cd["pop"] = f'{pop.get("adults",0)}a {pop.get("children",0)}c {pop.get("bots",0)}b'
                res = d.get("resources", {})
                cd["resources"] = " ".join(f'{k}:{v}' for k, v in res.items())
                h = d.get("housing", {})
                cd["beds"] = f'{h.get("occupiedBeds",0)}/{h.get("totalBeds",0)} homeless:{h.get("homeless",0)}'
                e = d.get("employment", {})
                cd["workers"] = f'{e.get("assigned",0)}/{e.get("vacancies",0)} idle:{e.get("unemployed",0)}'
                wb = d.get("wellbeing", {})
                cd["wellbeing"] = f'{wb.get("average",0)}/77 miserable:{wb.get("miserable",0)} critical:{wb.get("critical",0)}'
                dc = d.get("dc", {})
                if dc:
                    cd["dc"] = f'{dc["x"]},{dc["y"]},z{dc.get("z",0)} {dc.get("orientation","")} entrance:{dc.get("entranceX",0)},{dc.get("entranceY",0)}'
                compact_districts.append(cd)
            s["districts"] = compact_districts
        # flatten tree/crop species into CSV
        if "trees" in s and isinstance(s["trees"], dict):
            sp = s["trees"].get("species", [])
            s["trees"] = {"marked": s["trees"].get("markedGrown", 0), "seedling": s["trees"].get("markedSeedling", 0), "unmarked": s["trees"].get("unmarkedGrown", 0),
                          "species": [{k: v for k, v in x.items()} for x in sp]}
        if "crops" in s and isinstance(s["crops"], dict):
            sp = s["crops"].get("species", [])
            s["crops"] = {"ready": s["crops"].get("ready", 0), "growing": s["crops"].get("growing", 0),
                          "species": [{k: v for k, v in x.items()} for x in sp]}
        # flatten wellbeing categories
        if "wellbeing" in s and isinstance(s["wellbeing"], dict):
            cats = s["wellbeing"].get("categories", [])
            s["wellbeing"] = {"avg": s["wellbeing"].get("average", 0), "miserable": s["wellbeing"].get("miserable", 0), "critical": s["wellbeing"].get("critical", 0),
                              "categories": [{k: v for k, v in c.items()} for c in cats]}
        # flatten clusters
        for key in ("treeClusters", "foodClusters"):
            if key in s:
                s[key] = [{"x": c["x"], "y": c["y"], "z": c.get("z", 0), "grown": c.get("grown", 0), "total": c.get("total", 0), "species": ",".join(c.get("species", {}).keys())} for c in s[key]]

        # compact locations
        compact_locs = {}
        for name, loc in locations.items():
            sp = ",".join(loc.get("species", [])) if "species" in loc else ""
            note = loc.get("note", "")
            val = f'{loc["x"]},{loc["y"]},z{loc.get("z",0)}'
            if sp:
                val += " " + sp
            if note:
                val += " " + note
            compact_locs[name] = val

        return {"summary": summary, "goal": current_goal, "tasks": tasks, "locations": compact_locs}

    def set_location(self, name: str, x: int, y: int, z: int = 0, note: str = "") -> dict[str, int | str]:
        """Save a named location. Persists across sessions."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        locations = brain.get("locations", {})
        loc: dict[str, int | str] = {"x": int(x), "y": int(y), "z": int(z)}
        if note:
            loc["note"] = note
        locations[name] = loc
        brain["locations"] = locations
        _save_brain_file(brain)
        return {"saved": name, "x": loc["x"], "y": loc["y"], "z": loc["z"]}

    def remove_location(self, name: str) -> dict[str, Any]:
        """Remove a named location."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        locations = brain.get("locations", {})
        if name not in locations:
            return {"error": "not_found", "name": name, "available": list(locations.keys())}
        del locations[name]
        brain["locations"] = locations
        _save_brain_file(brain)
        return {"removed": name}

    def list_locations(self) -> dict[str, Any]:
        """List all saved locations."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        return brain.get("locations", {})

    def clear_brain(self) -> dict[str, str]:
        """Wipe memory for current settlement. Run brain again to start fresh."""
        self._ensure_settlement_dir()
        import shutil
        if os.path.isdir(_memory_dir) and _memory_dir != _MEMORY_BASE:
            shutil.rmtree(_memory_dir)
            return {"cleared": _memory_dir}
        return {"error": "no settlement memory to clear"}

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def _ensure_settlement_dir(self):
        """Set _memory_dir to the correct settlement folder. Call before any disk operation."""
        global _memory_dir
        if _memory_dir != _MEMORY_BASE:
            return  # already set by brain()
        try:
            r = self.s.get(f"{self.url}/api/settlement", timeout=5)
            name = _sanitize_name(r.json().get("name", "unknown"))
            _memory_dir = os.path.join(_MEMORY_BASE, name)
        except Exception:
            pass

    def add_task(self, action: str) -> dict[str, Any]:
        """Add a pending task to brain.toon. Returns the new task."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        tasks = brain.get("tasks", [])
        next_id = max((t["id"] for t in tasks), default=0) + 1
        task = {"id": next_id, "status": "pending", "action": action}
        tasks.append(task)
        brain["tasks"] = tasks
        _save_brain_file(brain)
        return task

    def update_task(self, id: int, status: str, error: str | None = None) -> dict[str, Any]:
        """Update task status. status: pending/active/done/failed. Optional error for failed."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        tasks = brain.get("tasks", [])
        for t in tasks:
            if t["id"] == id:
                t["status"] = status
                if error:
                    t["error"] = error
                elif "error" in t and status != "failed":
                    del t["error"]
                brain["tasks"] = tasks
                _save_brain_file(brain)
                return t
        return {"error": f"task {id} not found"}

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all tasks from brain.toon."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        return brain.get("tasks", [])

    def clear_tasks(self, status: str = "done") -> dict[str, Any]:
        """Remove tasks with given status (default: done). Returns count cleared."""
        self._ensure_settlement_dir()
        brain = _load_brain_file()
        tasks = brain.get("tasks", [])
        before = len(tasks)
        brain["tasks"] = [t for t in tasks if t["status"] != status]
        _save_brain_file(brain)
        return {"cleared": before - len(brain["tasks"]), "remaining": len(brain["tasks"])}

    # ------------------------------------------------------------------
    # Agent control
    # ------------------------------------------------------------------

    def agent_status(self) -> dict[str, Any]:
        """Get AI agent loop status."""
        return self._get("/api/agent/status")

    def agent_stop(self) -> dict[str, Any]:
        """Stop AI agent loop."""
        return self._post("/api/agent/stop", {})

    def find(self, source: str, name: str | None = None, x: int | None = None, y: int | None = None, radius: int = 20, limit: int = 0) -> dict[str, Any]:
        """Find entities from a source (buildings/trees/gatherables/beavers). Filters server-side."""
        params: dict[str, int | str] = {"limit": limit}
        if name:
            params["name"] = name
        if x is not None and y is not None:
            params["x"] = x
            params["y"] = y
            params["radius"] = radius
        return self._get(f"/api/{source}", params=params)


# ---------------------------------------------------------------------------
# Live dashboard (top subcommand)
# ---------------------------------------------------------------------------

_RST = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_WHT = "\033[37m"
_BRED = "\033[91m"
_BGRN = "\033[92m"
_BYEL = "\033[93m"
_BBLU = "\033[94m"
_BMAG = "\033[95m"
_BCYN = "\033[96m"

W = 86  # total width

# ensure UTF-8 output on Windows
import io, sys as _sys
if _sys.stdout.encoding != 'utf-8' and isinstance(_sys.stdout, io.TextIOWrapper):
    _sys.stdout.reconfigure(encoding='utf-8')


def _cv(val: float, warn: float, crit: float, fmt: str = ".0f") -> str:
    """color a value: green/yellow/red based on thresholds"""
    c = _BRED if val < crit else _BYEL if val < warn else _BGRN
    return f"{c}{_BOLD}{val:{fmt}}{_RST}"


def _bar(cur: float, mx: float, w: int = 12) -> str:
    """progress bar with gradient: ████░░░░"""
    if mx <= 0:
        return f"{_DIM}{'░' * w}{_RST}"
    ratio = max(0.0, min(cur / mx, 1.0))
    filled = int(ratio * w)
    c = _BRED if ratio < 0.25 else _BYEL if ratio < 0.5 else _BGRN
    return f"{c}{'█' * filled}{_DIM}{'░' * (w - filled)}{_RST}"


def _hline():
    return f" {_DIM}{'─' * W}{_RST}"


def _row(left: str, right: str | None = None, split: int = 43) -> str:
    """row with optional two-column layout. No side borders."""
    import re
    if right is None:
        return f"  {left}"
    else:
        plain_l = re.sub(r'\033\[[0-9;]*m', '', left)
        pad_l = max(0, split - len(plain_l))
        return f"  {left}{' ' * pad_l}  {right}"


def _top_render(summary: dict[str, Any] | None, wellbeing_data: dict[str, Any] | None = None, trees_data: list[dict[str, Any]] | None = None, crops_data: list[dict[str, Any]] | None = None, interval: int = 5, agent_data: dict[str, Any] | None = None, agent_turns: int = 5) -> None:
    if not summary:
        print(f"\n {_RED}-- game not reachable --{_RST}\n")
        return

    t = summary.get("time", {})
    w = summary.get("weather", {})
    day = t.get("dayNumber", 0)
    hazardous = w.get("isHazardous", False)
    temp_len = w.get("temperateWeatherDuration", 0)
    haz_len = w.get("hazardousWeatherDuration", 0)
    cday = w.get("cycleDay", 0)
    remaining = temp_len + haz_len - cday + 1 if hazardous else temp_len - cday + 1

    day_progress = t.get("dayProgress", 0)
    season_str = f"{_BRED}{_BOLD}DROUGHT{_RST}" if hazardous else f"{_BGRN}Temperate{_RST}"
    day_bar = _bar(day_progress, 1.0, 8)
    day_str = f"Day {_BCYN}{_BOLD}{day}{_RST} {day_bar}  {season_str} {_DIM}{cday}/{temp_len}+{haz_len}{_RST} ({_BOLD}{remaining}d{_RST})"

    # header
    print(f" {_DIM}{'─' * W}{_RST}")
    print(_row(f"{_BCYN}{_BOLD}Timberbot API{_RST}                            {day_str}"))
    print(_hline())

    # population
    districts = summary.get("districts", [])
    total_adults = sum(d.get("population", {}).get("adults", 0) for d in districts)
    total_children = sum(d.get("population", {}).get("children", 0) for d in districts)
    total_bots = sum(d.get("population", {}).get("bots", 0) for d in districts)
    total_pop = total_adults + total_children + total_bots

    resources = {}
    for d in districts:
        for good, val in d.get("resources", {}).items():
            amt = val.get("available", val) if isinstance(val, dict) else val
            resources[good] = resources.get(good, 0) + amt

    # aggregate housing/employment from per-district data
    occ_beds = sum(d.get("housing", {}).get("occupiedBeds", 0) for d in districts)
    tot_beds = sum(d.get("housing", {}).get("totalBeds", 0) for d in districts)
    assigned = sum(d.get("employment", {}).get("assigned", 0) for d in districts)
    vacancies = sum(d.get("employment", {}).get("vacancies", 0) for d in districts)
    unemployed = sum(d.get("employment", {}).get("unemployed", 0) for d in districts)
    wb_obj = summary.get("wellbeing", {})
    wb_avg = wb_obj.get("average", 0) if isinstance(wb_obj, dict) else 0
    critical = wb_obj.get("critical", 0) if isinstance(wb_obj, dict) else 0

    pop_parts = f"{_BOLD}{total_adults}{_RST} adults  {_BOLD}{total_children}{_RST} children"
    if total_bots:
        pop_parts += f"  {_BOLD}{total_bots}{_RST} bots"

    homeless = sum(d.get("housing", {}).get("homeless", 0) for d in districts)
    miserable = wb_obj.get("miserable", 0) if isinstance(wb_obj, dict) else 0
    science = summary.get("science", 0)
    idle_c = _BRED if unemployed == 0 else _BGRN if unemployed <= 4 else _BYEL
    crit_str = f"  {_BRED}{_BOLD}● {critical} critical{_RST}" if critical > 0 else ""
    homeless_str = f"  {_BRED}{_BOLD}{homeless} homeless{_RST}" if homeless > 0 else ""
    miserable_str = f"  {_BYEL}{miserable} miserable{_RST}" if miserable > 0 else ""

    print(_row(f"{_BCYN}{_BOLD}{total_pop}{_RST} beavers  {_DIM}({pop_parts}{_DIM}){_RST}", f"Beds {_BOLD}{occ_beds}{_RST}/{tot_beds}  Workers {_BOLD}{assigned}{_RST}/{vacancies}  Idle {idle_c}{_BOLD}{unemployed}{_RST}"))
    print(_row(f"Wellbeing {_bar(wb_avg, 77, 20)} {_cv(wb_avg, 8, 4, '.1f')}/77{crit_str}{miserable_str}{homeless_str}"))
    print(_hline())

    # food + water (left) | wellbeing categories (right)
    _EDIBLE = ["Berries", "Kohlrabi", "Bread", "Carrot", "CornRation", "AlgaeRation",
                "EggplantRation", "FermentedSoybean", "FermentedMushroom", "FermentedCassava",
                "Coffee", "MangroveFruit"]
    _RAW_CROPS = ["Soybean", "Corn", "Sunflower", "Eggplant", "Algae", "Cassava", "Mushroom"]
    total_food = sum(resources.get(g, 0) for g in _EDIBLE)
    _ = sum(resources.get(g, 0) for g in _RAW_CROPS)
    total_water = resources.get("Water", 0)
    food_days = round(total_food / total_pop, 1) if total_pop > 0 else 0
    water_days = round(total_water / (total_pop * 2), 1) if total_pop > 0 else 0

    food_items = [(g, resources.get(g, 0)) for g in _EDIBLE if resources.get(g, 0) > 0]
    _ = [(g, resources.get(g, 0)) for g in _RAW_CROPS if resources.get(g, 0) > 0]

    wb_cats = []
    # prefer categories from summary (avoids extra API call), fall back to separate wellbeing_data
    wb_source = wb_obj.get("categories", []) if isinstance(wb_obj, dict) and "categories" in wb_obj else (
        wellbeing_data.get("categories", []) if wellbeing_data and isinstance(wellbeing_data, dict) else [])
    for cat in wb_source:
        wb_cats.append((cat.get("group", "?"), cat.get("current", 0), cat.get("max", 0)))

    # food header
    left_lines = [f"{_BCYN}{_BOLD}FOOD{_RST}  {_cv(food_days, 3, 1, '.1f')} days  {_DIM}({total_food} total){_RST}"]
    for i, (g, amt) in enumerate(food_items):
        branch = "└─" if i == len(food_items) - 1 else "├─"
        left_lines.append(f"  {_DIM}{branch}{_RST} {g:16s} {_BOLD}{amt:>5}{_RST}")

    left_lines.append(f"{_BCYN}{_BOLD}WATER{_RST} {_cv(water_days, 2, 0.5, '.1f')} days  {_BBLU}{_BOLD}{total_water}{_RST}")
    left_lines.append("")

    right_lines = [f"{_BCYN}{_BOLD}WELLBEING{_RST}"]
    for g, cur, mx in wb_cats:
        right_lines.append(f"{g:13s} {_bar(cur, mx, 10)} {_cv(cur, mx * 0.5, mx * 0.1, '.1f')}{_DIM}/{mx:.0f}{_RST}")

    max_rows = max(len(left_lines), len(right_lines))
    for i in range(max_rows):
        l = left_lines[i] if i < len(left_lines) else ""
        r = right_lines[i] if i < len(right_lines) else ""
        print(_row(l, r))

    print(_hline())

    # materials (left) | alerts + projections (right)
    mat_lines = [f"{_BCYN}{_BOLD}MATERIALS{_RST}"]
    for good in ["Log", "Plank", "Gear", "ScrapMetal", "MetalPart"]:
        if good in resources:
            mat_lines.append(f"  {good:16s} {_BOLD}{resources[good]:>5}{_RST}")
    mat_lines.append(f"  {'Science':16s} {_BCYN}{_BOLD}{science:>5}{_RST}")

    alerts_obj = summary.get("alerts", {})
    alert_lines = [f"{_BCYN}{_BOLD}ALERTS{_RST}"]
    if isinstance(alerts_obj, dict):
        for k, v in alerts_obj.items():
            if v > 0:
                alert_lines.append(f"  {_BYEL}⚠ {v} {k}{_RST}")
    if len(alert_lines) == 1:
        alert_lines.append(f"  {_BGRN}● all clear{_RST}")

    max_rows = max(len(mat_lines), len(alert_lines))
    for i in range(max_rows):
        l = mat_lines[i] if i < len(mat_lines) else ""
        r = alert_lines[i] if i < len(alert_lines) else ""
        print(_row(l, r))

    # trees section. prefer per-species from summary, fall back to full trees_data
    trees_obj = summary.get("trees", {})
    tree_species = trees_obj.get("species", []) if isinstance(trees_obj, dict) else []
    if tree_species:
        tree_counts = {}
        for s in tree_species:
            n = s.get("name", "")
            tree_counts[n] = {"marked_grown": s.get("markedGrown", 0), "unmarked_grown": s.get("unmarkedGrown", 0), "seedling": s.get("seedling", 0)}
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
        print(_hline())
        tree_left = [f"{_BCYN}{_BOLD}TREES{_RST}"]
        tree_right = []
        total_chop = sum(c["marked_grown"] for c in tree_counts.values())
        total_unmarked = sum(c["unmarked_grown"] for c in tree_counts.values())
        total_seed = sum(c["seedling"] for c in tree_counts.values())
        tree_left.append(f"  {_BGRN}{_BOLD}{total_chop}{_RST} choppable  {_DIM}{total_unmarked} unmarked  {total_seed} seedlings{_RST}")
        for name in sorted(tree_counts, key=lambda n: tree_counts[n]["marked_grown"], reverse=True):
            c = tree_counts[name]
            if c["marked_grown"] + c["unmarked_grown"] + c["seedling"] > 0:
                tree_left.append(f"  {_DIM}{name:10s}{_RST} {_BGRN}{_BOLD}{c['marked_grown']:>4}{_RST} marked  {_DIM}{c['unmarked_grown']} free  {c['seedling']} growing{_RST}")
        for i in range(len(tree_left)):
            l = tree_left[i] if i < len(tree_left) else ""
            r = tree_right[i] if i < len(tree_right) else ""
            print(_row(l, r))

    # crops section. prefer per-species from summary, fall back to full crops_data
    crops_obj = summary.get("crops", {})
    crop_species = crops_obj.get("species", []) if isinstance(crops_obj, dict) else []
    if crop_species:
        crop_counts = {}
        for s in crop_species:
            n = s.get("name", "")
            crop_counts[n] = {"alive": s.get("ready", 0) + s.get("growing", 0), "grown": s.get("ready", 0)}
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
        print(_hline())
        crop_left = [f"{_BCYN}{_BOLD}CROPS{_RST}  {_DIM}(in ground){_RST}"]
        crop_right = []
        items = sorted(crop_counts.items(), key=lambda x: x[1]["alive"], reverse=True)
        for name, c in items:
            grown_c = _BGRN if c["grown"] > 0 else _DIM
            crop_left.append(f"  {name:14s} {grown_c}{_BOLD}{c['grown']:>4}{_RST} ready  {_DIM}{c['alive'] - c['grown']} growing{_RST}")
        for i in range(len(crop_left)):
            l = crop_left[i] if i < len(crop_left) else ""
            r = crop_right[i] if i < len(crop_right) else ""
            print(_row(l, r))

    # districts
    if len(districts) > 0:
        print(_hline())
        print(_row(f"{_BCYN}{_BOLD}DISTRICTS{_RST}"))
        for d in districts:
            name = d.get("name", "?")
            pop = d.get("population", {})
            dpop = pop.get("adults", 0) + pop.get("children", 0) + pop.get("bots", 0)
            dres = d.get("resources", {})
            dwater = dres.get("Water", 0)
            dw = dwater.get("available", 0) if isinstance(dwater, dict) else dwater
            dlog = dres.get("Log", 0)
            dl = dlog.get("available", 0) if isinstance(dlog, dict) else dlog
            print(_row(f"  {name:16s} {_BOLD}{dpop:>3}{_RST} pop   Water {_BBLU}{_BOLD}{dw:>4}{_RST}   Log {_BOLD}{dl:>4}{_RST}"))

    # agent status (if running or recently completed)
    if agent_data and isinstance(agent_data, dict):
        s = agent_data.get("status", "idle")
        if s != "idle":
            print(_hline())
            status_colors = {"gatheringstate": _BYEL, "thinking": _BMAG,
                             "executing": _BCYN, "done": _BGRN, "error": _BRED}
            sc = status_colors.get(s, _DIM)
            turn = agent_data.get("turn", 0)
            total = agent_data.get("totalTurns", 0)
            binary = agent_data.get("binary", "")
            model = agent_data.get("model", "")
            cur_cmd = agent_data.get("currentCmd", "")
            turn_bar = _bar(turn, total, 16) if total > 0 else ""
            model_short = model.replace("claude-", "").replace("-20251001", "") if model else binary
            goal = agent_data.get("goal", "")
            print(_row(f"{_BMAG}{_BOLD}AGENT{_RST}  {sc}{_BOLD}{s}{_RST}  turn {_BOLD}{turn}{_RST}/{total}  {turn_bar}", f"{_DIM}{model_short}{_RST}"))
            if goal:
                print(_row(f"  {_DIM}goal:{_RST} {_BOLD}{goal[:65]}{_RST}"))

            # show what's happening right now
            if cur_cmd and s in ("gatheringstate", "thinking", "executing"):
                print(_row(f"  {_BYEL}> {cur_cmd}{_RST}"))

            # turn history
            history = agent_data.get("history", [])
            visible = history[-8:]  # last 8 turns
            for rec in visible:
                tn = rec.get("turn", 0)
                _ok = rec.get("ok", 0)
                failed = rec.get("failed", 0)
                secs = rec.get("seconds", 0)
                cmds = rec.get("commands", [])
                err = rec.get("error", "")
                # format: turn N  12s  3ok  set_speed | buildings | place_building
                cmd_names = []
                for c in cmds:
                    # "ok: set_speed speed:1" -> "set_speed speed:1"
                    # "FAIL: place_building ..." -> "FAIL place_building ..."
                    if c.startswith("ok: "):
                        cmd_names.append(f"{_BCYN}{c[4:]}{_RST}")
                    elif c.startswith("FAIL: "):
                        cmd_names.append(f"{_BRED}{c[6:]}{_RST}")
                    else:
                        cmd_names.append(c)
                summary_str = "  ".join(cmd_names[:4])
                extra = f" {_DIM}+{len(cmd_names)-4}{_RST}" if len(cmd_names) > 4 else ""
                fail_str = f" {_BRED}{failed}fail{_RST}" if failed else ""
                time_str = f"{secs:.0f}s" if secs >= 1 else "<1s"
                if err:
                    print(_row(f"  {_DIM}t{tn}{_RST} {_DIM}{time_str:>4}{_RST}  {_RED}{err[:60]}{_RST}"))
                else:
                    print(_row(f"  {_DIM}t{tn}{_RST} {_DIM}{time_str:>4}{_RST}{fail_str}  {summary_str}{extra}"))

            err = agent_data.get("lastError", "")
            if err and s == "error":
                print(_row(f"  {_RED}{err[:70]}{_RST}"))

    print(f" {_DIM}{'─' * W}{_RST}")
    agent_running = agent_data and isinstance(agent_data, dict) and agent_data.get("status") not in ("idle", "done", "error", None)
    if not agent_running:
        keys = f"[s]tart({agent_turns}t)  [+/-]turns  [0-3]speed  [q]uit"
    else:
        keys = f"[x]stop  [0-3]speed  [q]uit"
    print(f"  {_DIM}{keys}  ·  refreshing every {interval}s{_RST}")


def _top(interval: int = 5) -> None:
    import time
    import sys
    
    is_win = sys.platform == "win32"
    if is_win:
        import msvcrt
    else:
        import select
        import tty
        import termios

    bot = Timberbot(json_mode=True)

    if not bot.ping():
        print(f"  {_RED}cannot reach Timberbot on port 8085{_RST}")
        print(f"  {_DIM}start Timberborn with the mod loaded{_RST}\n")
        sys.exit(1)

    agent_turns = 5
    agent_model = "claude-haiku-4-5-20251001"

    old_settings = None
    if not is_win:
        old_settings = termios.tcgetattr(sys.stdin)  # pyright: ignore[reportPossiblyUnboundVariable]
        tty.setcbreak(sys.stdin.fileno())  # pyright: ignore[reportPossiblyUnboundVariable]

    def _drain_key():
        """Non-blocking read of a keypress, or None."""
        if is_win:
            if msvcrt.kbhit():  # pyright: ignore[reportPossiblyUnboundVariable]
                return msvcrt.getch()  # pyright: ignore[reportPossiblyUnboundVariable]
            return None
        else:
            rlist, _, _ = select.select([sys.stdin], [], [], 0)  # pyright: ignore[reportPossiblyUnboundVariable]
            if rlist:
                ch = sys.stdin.read(1)
                return ch.encode('utf-8')
            return None

    try:
        while True:
            try:
                summary = bot.summary()
            except Exception:
                summary = None
            try:
                agent = bot._get_json("/api/agent/status")
            except Exception:
                agent = None
            print("\033[2J\033[H", end="")
            print()
            _top_render(summary, interval=interval, agent_data=agent, agent_turns=agent_turns)

            # poll for keypress during sleep interval
            deadline = time.time() + interval
            while time.time() < deadline:
                key = _drain_key()
                if key is None:
                    time.sleep(0.1)
                    continue
                ch = key.lower()
                if ch == b'q':
                    print(f"\n  {_DIM}bye!{_RST}\n")
                    return
                elif ch == b's':
                    agent_st = agent.get("status") if agent else "idle"
                    if agent_st in ("idle", "done", "error", None):
                        try:
                            _ = bot._post("/api/agent/start", {"binary": "claude", "turns": agent_turns, "model": agent_model, "interval": 5, "timeout": 300})
                            # force refresh
                            break
                        except Exception:
                            pass
                elif ch == b'x':
                    try:
                        _ = bot._post("/api/agent/stop", {})
                    except Exception:
                        pass
                    break
                elif ch == b'+' or ch == b'=':
                    agent_turns = min(agent_turns + 5, 100)
                    break
                elif ch == b'-':
                    agent_turns = max(agent_turns - 5, 1)
                    break
                elif ch in (b'0', b'1', b'2', b'3'):
                    try:
                        _ = bot.set_speed(int(ch))
                    except Exception:
                        pass
                    break
    except KeyboardInterrupt:
        print(f"\n  {_DIM}bye!{_RST}\n")
    finally:
        if old_settings and not is_win:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)  # pyright: ignore[reportPossiblyUnboundVariable]


# Workforce manager (manage subcommand)
# ---------------------------------------------------------------------------

_ESSENTIAL = {"FarmHouse", "DeepWaterPump", "LumberjackFlag", "ScavengerFlag",
              "GathererFlag", "BreedingPod", "SmallTank", "MediumTank", "LargeTank"}
_LOW_PRIORITY = ["Inventor", "Metalsmith", "BotPartFactory", "BotAssembler",
                 "GearWorkshop", "Scratcher", "FluidDump", "Forester",
                 "IndustrialLumberMill", "LargePowerWheel", "DistrictCenter"]


def _is_essential(name: str) -> bool:
    return any(e in name for e in _ESSENTIAL)


def _manage() -> None:
    bot = Timberbot(json_mode=True)

    if not bot.ping():
        print(f"  {_RED}cannot reach Timberbot on port 8085{_RST}")
        sys.exit(1)

    print(f"  {_BOLD}{_BMAG}timberbot manage{_RST}  {_DIM}keeping 1-4 idle haulers. ctrl+c to stop{_RST}\n")

    # track what we paused so we unpause in reverse order
    paused_by_us = []

    try:
        while True:
            try:
                summary = bot.summary()
                idle = sum(d.get("employment", {}).get("unemployed", 0) for d in summary.get("districts", []))
                bldgs = bot.buildings()
                blist: list[dict[str, Any]] = bldgs.get("buildings", []) if isinstance(bldgs, dict) else bldgs
            except Exception:
                print(f"  {_RED}-- connection lost --{_RST}")
                time.sleep(10)
                continue

            idle_color = _BRED if idle == 0 else _BGRN if idle <= 4 else _BYEL
            ts = time.strftime("%H:%M:%S")

            if idle == 0:
                # find something to pause from low-priority list
                acted = False
                for prio_name in _LOW_PRIORITY:
                    for b in blist:
                        if (prio_name in b.get("name", "") and
                                not b.get("paused") and
                                b.get("assignedWorkers", 0) > 0 and
                                not _is_essential(b.get("name", ""))):
                            _ = bot.pause_building(b["id"])
                            paused_by_us.append(b["id"])
                            print(f"  {ts}  {_BRED}0 idle{_RST}  paused {_BYEL}{b['name']}{_RST} id:{b['id']}")
                            acted = True
                            break
                    if acted:
                        break
                if not acted:
                    print(f"  {ts}  {_BRED}0 idle{_RST}  {_DIM}nothing left to pause{_RST}")

            elif idle > 4 and paused_by_us:
                # unpause the last thing we paused
                bid = paused_by_us.pop()
                name = "?"
                for b in blist:
                    if b.get("id") == bid:
                        name = b.get("name", "?")
                        break
                _ = bot.unpause_building(bid)
                print(f"  {ts}  {_BYEL}{idle} idle{_RST}  unpaused {_BGRN}{name}{_RST} id:{bid}")

            else:
                print(f"  {ts}  {idle_color}{idle} idle{_RST}  {_DIM}ok{_RST}")

            time.sleep(10)
    except KeyboardInterrupt:
        print(f"\n  {_DIM}bye!{_RST}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

import inspect







def _cast(a: str) -> bool | int | float | str:
    if a.lower() == "true":
        return True
    if a.lower() == "false":
        return False
    try:
        return int(a)
    except ValueError:
        try:
            return float(a)
        except ValueError:
            return a


_SAVES_DIR = _saves_dir()


def _start_agent(args: list[str]) -> None:
    """Start AI agent loop via the mod's HTTP API.

    Usage: timberbot.py start binary:claude [turns:5] [model:MODEL] [interval:10] [timeout:120] [goal:"survive and grow"]
                              binary:custom command:"aider --system-prompt-file {skill} {prompt}"
    """
    binary = "claude"
    turns = 5
    model = None
    interval = 10
    proc_timeout = 120
    goal = None
    command = None

    for a in args:
        if ":" in a:
            key, val = a.split(":", 1)
            if key == "binary":
                binary = val
            elif key == "turns":
                try: turns = int(val)
                except ValueError: pass
            elif key == "model":
                model = val
            elif key == "interval":
                try: interval = int(val)
                except ValueError: pass
            elif key == "timeout":
                try: proc_timeout = int(val)
                except ValueError: pass
            elif key == "goal":
                goal = val
            elif key == "command":
                command = val

    bot = Timberbot(json_mode=True)
    if not bot.ping():
        print(f"  {_RED}error: game not reachable. launch first with: timberbot.py launch settlement:<name>{_RST}", file=sys.stderr)
        sys.exit(1)

    data = {"binary": binary, "turns": turns, "interval": interval, "timeout": proc_timeout}
    if model:
        data["model"] = model
    if goal:
        data["goal"] = goal
    if command:
        data["command"] = command

    try:
        _ = bot._post("/api/agent/start", data)
    except TimberbotError as e:
        print(f"  {_RED}error: {e.error}{_RST}", file=sys.stderr)
        sys.exit(1)

    _label = command or binary
    print(f"  {_BGRN}started{_RST} binary={binary} turns={turns} interval={interval}s")
    if command:
        print(f"  {_DIM}command: {command}{_RST}")
    print(f"  {_DIM}use 'timberbot.py top' to monitor{_RST}")


def _launch(args: list[str]) -> None:
    """Prepare a save launch via autoload.json.

    Usage: timberbot.py launch settlement:<name> [save:<filename>] [timeout:120]
    """
    settlement = None
    save_name = None
    timeout = 120

    for a in args:
        if ":" in a:
            key, val = a.split(":", 1)
            if key == "settlement":
                settlement = val
            elif key == "save":
                save_name = val
            elif key == "timeout":
                try:
                    timeout = int(val)
                except ValueError:
                    pass

    if not settlement:
        print(f"  {_RED}error: settlement:<name> is required{_RST}", file=sys.stderr)
        print("  usage: timberbot.py launch settlement:<name> [save:<filename>] [timeout:120]", file=sys.stderr)
        sys.exit(1)

    # validate settlement exists
    sdir = os.path.join(_SAVES_DIR, settlement)
    if not os.path.isdir(sdir):
        print(f"  {_RED}error: settlement folder not found: {sdir}{_RST}", file=sys.stderr)
        avail = [d for d in os.listdir(_SAVES_DIR)
                 if os.path.isdir(os.path.join(_SAVES_DIR, d))]
        if avail:
            print(f"  available: {', '.join(sorted(avail))}", file=sys.stderr)
        sys.exit(1)

    # validate or pick save
    if save_name:
        # strip .timber extension if provided
        if save_name.endswith(".timber"):
            save_name = save_name[:-7]
        spath = os.path.join(sdir, save_name + ".timber")
        if not os.path.isfile(spath):
            print(f"  {_RED}error: save not found: {spath}{_RST}", file=sys.stderr)
            sys.exit(1)
    else:
        # pick most recent .timber file
        timbers = [f for f in os.listdir(sdir) if f.endswith(".timber")]
        if not timbers:
            print(f"  {_RED}error: no saves in {sdir}{_RST}", file=sys.stderr)
            sys.exit(1)
        timbers.sort(key=lambda f: os.path.getmtime(os.path.join(sdir, f)), reverse=True)
        save_name = timbers[0][:-7]  # strip .timber

    # write autoload.json for the mod to pick up (avoids Steam CLI arg dialog)
    mod_dir = _mod_dir()
    autoload = {"settlement": settlement, "save": save_name}
    with open(os.path.join(mod_dir, "autoload.json"), "w") as f:
        json.dump(autoload, f)

    if platform.system() == "Darwin":
        print(f"  {_BGRN}autoload prepared{_RST}  {settlement} / {save_name}")
        print(f"  {_DIM}open Timberborn manually on macOS and the mod will load this save from autoload.json{_RST}")
        return

    # kill existing Timberborn process if running, wait until it's gone
    try:
        r = subprocess.run(["taskkill", "/f", "/im", "Timberborn.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            print(f"  {_DIM}waiting for Timberborn to exit...{_RST}")
            for _ in range(30):
                time.sleep(1)
                check = subprocess.run(["tasklist", "/fi", "imagename eq Timberborn.exe"],
                                       capture_output=True, text=True)
                if "Timberborn.exe" not in check.stdout:
                    break
            time.sleep(2)  # extra buffer for file locks / port release
    except Exception:
        pass

    # launch via Steam. try direct exe first, fall back to protocol handler
    print(f"  {_BOLD}launching{_RST} {settlement} / {save_name}")
    steam_exe = r"C:\Games\Steam\steam.exe"
    if os.path.exists(steam_exe):
        _ = subprocess.Popen([steam_exe, "-applaunch", "1062090"])
    else:
        # fall back to protocol handler
        _ = subprocess.Popen(["cmd.exe", "/c", "start", "steam://rungameid/1062090"], shell=False)

    # wait for Timberborn.exe to appear (confirms Steam actually launched it)
    print(f"  {_DIM}waiting for Timberborn.exe to start...{_RST}")
    exe_started = False
    for _ in range(30):
        time.sleep(2)
        check = subprocess.run(["tasklist", "/fi", "imagename eq Timberborn.exe"],
                               capture_output=True, text=True)
        if "Timberborn.exe" in check.stdout:
            exe_started = True
            break
    if not exe_started:
        print(f"  {_RED}error: Timberborn.exe did not start after 60s. Is Steam running?{_RST}", file=sys.stderr)
        sys.exit(1)

    # poll until the mod's HTTP API responds
    print(f"  {_DIM}waiting for game to load (timeout {timeout}s)...{_RST}")
    start = time.time()
    bot = Timberbot(json_mode=True)
    while time.time() - start < timeout:
        try:
            s = bot.summary()
            name = ""
            for d in s.get("districts", []):
                if d.get("name"):
                    name = d["name"]
                    break
            print(f"  {_BGRN}ready{_RST}  settlement: {name or settlement}")
            return
        except Exception:
            time.sleep(3)

    print(f"  {_RED}timeout after {timeout}s. game may still be loading{_RST}", file=sys.stderr)
    sys.exit(1)


def _method_params(method: Any) -> list[str]:
    """Get parameter names (excluding self) for a method."""
    sig = inspect.signature(method)
    return [p.name for p in sig.parameters.values() if p.name != "self"]


def _format_usage(name: str, method: Any) -> str:
    """Format usage string showing key:value pairs."""
    params = []
    sig = inspect.signature(method)
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        if p.default is inspect.Parameter.empty:
            params.append(f"{p.name}:VALUE")
        else:
            params.append(f"[{p.name}:{p.default}]")
    return f"  {name} {' '.join(params)}"


def main():
    help_mode = "--help" in sys.argv or "-h" in sys.argv
    json_mode = "--json" in sys.argv
    host_override = None
    port_override = None
    for a in sys.argv[1:]:
        if a.startswith("--host="):
            host_override = a.split("=", 1)[1]
        elif a.startswith("--port="):
            try: port_override = int(a.split("=", 1)[1])
            except ValueError: pass
    skip = {"--", "--json", "--help", "-h"}
    raw_args = [a for a in sys.argv[1:] if a not in skip and not a.startswith("--host=") and not a.startswith("--port=")]

    if not raw_args:
        bot = Timberbot()
        print("usage: timberbot.py <method> key:value ...")
        print()
        print("methods:")
        for name in sorted(dir(bot)):
            if name.startswith("_"):
                continue
            method = getattr(bot, name)
            if callable(method):
                doc = (method.__doc__ or "").split("\n")[0].strip()
                print(f"  {name:30s} {doc}")
                usage = _format_usage(name, method)
                if "VALUE" in usage:
                    print(f"    {usage.strip()}")
        print(f"\n  {'top':30s} live colony dashboard")
        print(f"  {'manager':30s} auto-manage haulers (keep 1-4 idle)")
        if platform.system() == "Darwin":
            print(f"  {'launch':30s} prepare autoload.json, then open Timberborn manually")
        else:
            print(f"  {'launch':30s} prepare autoload and launch the game (manual open on macOS)")
        print(f"  {'start':30s} start the built-in interactive agent")
        sys.exit(0 if help_mode else 1)

    method_name = raw_args[0]
    args = raw_args[1:]

    if help_mode:
        bot = Timberbot()
        if not hasattr(bot, method_name):
            print(f"error: unknown method '{method_name}'", file=sys.stderr)
            sys.exit(1)
        method = getattr(bot, method_name)
        if not callable(method):
            print(f"'{method_name}' is a property or not callable.")
            sys.exit(0)
        doc = method.__doc__ or "No documentation available."
        print(f"Method: {method_name}")
        print("-" * (8 + len(method_name)))
        print(doc.strip())
        print()
        print(f"Usage:\n  {_format_usage(method_name, method).strip()}")
        sys.exit(0)

    if method_name == "top":
        # parse optional interval: top interval:3
        interval = 5
        for a in args:
            if a.startswith("interval:"):
                try: interval = int(a.split(":", 1)[1])
                except ValueError: pass
        _top(interval)
        return

    if method_name == "manager":
        _manage()
        return

    if method_name == "launch":
        _launch(args)
        return

    if method_name == "start":
        _start_agent(args)
        return

    bot = Timberbot(host=host_override, port=port_override, json_mode=json_mode)

    if not hasattr(bot, method_name):
        print(f"error: unknown method '{method_name}'", file=sys.stderr)
        sys.exit(1)

    method = getattr(bot, method_name)
    if not callable(method):
        print(json.dumps(method, indent=2))
        sys.exit(0)

    params = _method_params(method)
    kwargs = {}
    for a in args:
        if ":" in a:
            key, val = a.split(":", 1)
            kwargs[key] = _cast(val)
        else:
            print(f"error: expected key:value, got '{a}'", file=sys.stderr)
            print(f"usage: {_format_usage(method_name, method).strip()}", file=sys.stderr)
            sys.exit(1)

    bad = [k for k in kwargs if k not in params]
    if bad:
        print(f"error: unknown parameter{'s' if len(bad) > 1 else ''} {', '.join(bad)} for '{method_name}'", file=sys.stderr)
        print(f"valid parameters: {', '.join(params) if params else '(none)'}", file=sys.stderr)
        print(f"usage: {_format_usage(method_name, method).strip()}", file=sys.stderr)
        sys.exit(1)

    try:
        result = method(**kwargs)
    except TimberbotError as e:
        if json_mode:
            print(json.dumps(e.response, indent=2), file=sys.stderr)
        else:
            try:
                import toons  # pyright: ignore[reportMissingImports]
                print(toons.dumps(e.response), file=sys.stderr)
            except ImportError:
                print(json.dumps(e.response, indent=2), file=sys.stderr)
        sys.exit(1)
    if isinstance(result, str):
        print(result)
    elif isinstance(result, dict) and result.get("rendered"):
        pass  # map() already printed to terminal
    elif json_mode:
        print(json.dumps(result, indent=2))
    else:
        try:
            import toons  # pyright: ignore[reportMissingImports]
            print(toons.dumps(result))
        except ImportError:
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
