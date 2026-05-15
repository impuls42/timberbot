"""HTTP client for the Timberbot mod (port 8085 by default).

All data processing happens server-side in the C# mod. This client sends a
`format` query parameter (`toon` or `json`) and passes the response straight
through. There is no client-side transformation of API data.

The class also exposes per-settlement persistent memory helpers (brain.toon,
locations, tasks). Those methods delegate to a lazily-constructed
`tbot.state.SettlementContext` rather than mutating module-level globals.
"""
from __future__ import annotations

from typing import Any

import requests

from tbot.api.exceptions import TimberbotError
from tbot.api.models._generated import (
    Alerts, BeaverList, BuildingList, CropList, DistrictList, Distribution,
    FoodClusters, GatherableList, Notifications, Population, PowerNetworks,
    Prefabs, Resources, Science, SettlementName, Speed, Summary, Tiles,
    Time, TreeClusters, TreeList, Weather, WebhookList, WellbeingReport,
    WorkHours,
)
from tbot.settings import resolve_endpoint
from tbot.state import SettlementContext, compact_locations, compact_summary


class TimberbotClient:
    """Client for Timberbot API (port 8085)."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        json_mode: bool = False,
        write_timeout: int = 60,
        settlement_context: SettlementContext | None = None,
    ) -> None:
        host, port = resolve_endpoint(host, port)
        self.host = host
        self.port = port
        self.url = f"http://{host}:{port}"
        self._format = "json" if json_mode else "toon"
        self._write_timeout = write_timeout
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        self._ctx: SettlementContext | None = settlement_context

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """True if the Timberbot mod is reachable."""
        try:
            return bool(self._get_json("/api/ping").get("ready", False))
        except (requests.ConnectionError, requests.Timeout):
            return False

    def settlement(self) -> SettlementName:
        """The current settlement's metadata (`{name: ...}`)."""
        return SettlementName.model_validate(self._get_json("/api/settlement"))

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def register_webhook(self, url: str, events: list[str] | None = None) -> dict[str, Any]:
        """Register a webhook URL to receive push notifications for game events.

        events: list of event names to subscribe to (None = all events).
        Available: drought.start, drought.end, building.placed, building.demolished,
                   beaver.born, beaver.died, day.start, night.start
        """
        data: dict[str, Any] = {"url": url}
        if events:
            data["events"] = events
        return self._post("/api/webhooks", data)

    def unregister_webhook(self, id: int) -> dict[str, Any]:
        """Unregister a webhook by ID."""
        return self._post("/api/webhooks/delete", {"id": id})

    def list_webhooks(self) -> WebhookList:
        """List all registered webhooks."""
        return WebhookList.model_validate(self._get_json("/api/webhooks"))

    # ------------------------------------------------------------------
    # Read state
    # ------------------------------------------------------------------

    def summary(self) -> Summary:
        """Full snapshot: time + weather + districts with resources and population."""
        return Summary.model_validate(self._get_json("/api/summary"))

    def time(self) -> Time:
        """Game time: {dayNumber, dayProgress, partialDayNumber}."""
        return Time.model_validate(self._get_json("/api/time"))

    def weather(self) -> Weather:
        """Weather: {cycle, cycleDay, isHazardous, temperateWeatherDuration, hazardousWeatherDuration}."""
        return Weather.model_validate(self._get_json("/api/weather"))

    def population(self) -> Population:
        """Beaver counts: [{district, adults, children, bots}]."""
        return Population.model_validate(self._get_json("/api/population"))

    def resources(self) -> Resources:
        """Resource stocks: {districtName: {goodName: {available, all}}}."""
        return Resources.model_validate(self._get_json("/api/resources"))

    def districts(self) -> DistrictList:
        """Districts: [{name, population: {adults, children, bots}, resources: {...}}]."""
        return DistrictList.model_validate(self._get_json("/api/districts"))

    def buildings(
        self,
        limit: int = 0,
        offset: int = 0,
        detail: str = "basic",
        id: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> BuildingList:
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
        return BuildingList.model_validate(self._get_json("/api/buildings", params=params))

    def buildings_v2(
        self,
        limit: int = 0,
        offset: int = 0,
        detail: str = "basic",
        id: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> BuildingList:
        """Compatibility alias for buildings()."""
        return self.buildings(
            limit=limit, offset=offset, detail=detail, id=id,
            name=name, x=x, y=y, radius=radius,
        )

    def trees(
        self, limit: int = 0, offset: int = 0, name: str = "",
        x: int = 0, y: int = 0, radius: int = 0,
    ) -> TreeList:
        """Trees: [{id, name, x, y, z, marked, alive, grown, growth}]. name: species filter. x/y/radius: proximity."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return TreeList.model_validate(self._get_json("/api/trees", params=params))

    def crops(
        self, limit: int = 0, offset: int = 0, name: str = "",
        x: int = 0, y: int = 0, radius: int = 0,
    ) -> CropList:
        """Crops in the ground: [{id, name, x, y, z, marked, alive, grown, growth}]. name: crop filter. x/y/radius: proximity."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return CropList.model_validate(self._get_json("/api/crops", params=params))

    def gatherables(
        self, limit: int = 0, offset: int = 0, name: str = "",
        x: int = 0, y: int = 0, radius: int = 0,
    ) -> GatherableList:
        """All gatherable resources (berry bushes etc): [{id, name, x, y, z, alive}]. name/x/y/radius: filters."""
        params: dict[str, int | str] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if x and y:
            params["x"] = x
            params["y"] = y
            if radius:
                params["radius"] = radius
        return GatherableList.model_validate(self._get_json("/api/gatherables", params=params))

    def beavers(
        self, limit: int = 0, offset: int = 0, detail: str = "basic", id: int = 0,
        name: str = "", x: int = 0, y: int = 0, radius: int = 0,
    ) -> BeaverList:
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
        return BeaverList.model_validate(self._get_json("/api/beavers", params=params))

    def workhours(self) -> WorkHours:
        """Work schedule: {endHours, areWorkingHours, hoursPassedToday}."""
        return WorkHours.model_validate(self._get_json("/api/workhours"))

    def migrate(self, from_district: str, to_district: str, count: int = 1) -> dict[str, Any]:
        """Move beavers between districts."""
        return self._post("/api/district/migrate", {
            "from": from_district, "to": to_district, "count": count,
        })

    def set_workhours(self, end_hours: int) -> dict[str, Any]:
        """Set when work ends (1-24). Beavers work from dawn until endHours."""
        return self._post("/api/workhours", {"endHours": end_hours})

    def science(self) -> Science:
        """Science points and unlockable buildings: {points, unlockables: [{name, cost, unlocked}]}."""
        return Science.model_validate(self._get_json("/api/science"))

    def wellbeing(self) -> WellbeingReport:
        """Population wellbeing breakdown by category: {beavers, categories: [{group, current, max, needs}]}."""
        return WellbeingReport.model_validate(self._get_json("/api/wellbeing"))

    def unlock_building(self, building: str) -> dict[str, Any]:
        """Unlock a building using science points."""
        return self._post("/api/science/unlock", {"building": building})

    def notifications(self) -> Notifications:
        """Game notification history: [{subject, description, entityId, cycle, cycleDay}]."""
        return Notifications.model_validate(self._get_json("/api/notifications"))

    def alerts(self) -> Alerts:
        """Alerts: unstaffed, unpowered, unreachable, status issues."""
        return Alerts.model_validate(self._get_json("/api/alerts"))

    def distribution(self) -> Distribution:
        """Distribution settings per district: [{district, goods: [{good, importOption, exportThreshold}]}]."""
        return Distribution.model_validate(self._get_json("/api/distribution"))

    def set_distribution(
        self, district: str, good: str, import_option: str = "", export_threshold: int = -1,
    ) -> dict[str, Any]:
        """Set import/export for a good in a district. import_option: Forced, Auto, None."""
        return self._post("/api/distribution", {
            "district": district, "good": good,
            "import": import_option, "exportThreshold": export_threshold,
        })

    def prefabs(self) -> Prefabs:
        """Available building templates: [{name, sizeX, sizeY, sizeZ}]."""
        return Prefabs.model_validate(self._get_json("/api/prefabs"))

    def power(self) -> PowerNetworks:
        """Power networks: [{id, supply, demand, buildings}]."""
        return PowerNetworks.model_validate(self._get_json("/api/power"))

    def speed(self) -> Speed:
        """Current game speed: {speed: 0-3}."""
        return Speed.model_validate(self._get_json("/api/speed"))

    def tiles(self, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0) -> Tiles:
        """Tile data for a region: terrain, water, occupants, moisture, contamination. No args = map size only."""
        return Tiles.model_validate(self._get_json("/api/tiles", {"x1": x1, "y1": y1, "x2": x2, "y2": y2}))

    # ------------------------------------------------------------------
    # Write actions
    # ------------------------------------------------------------------

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

    def benchmark(self, iterations: int = 100, **kwargs: Any) -> dict[str, Any]:
        """Run the debug benchmark loop. Gated by `debugEndpointEnabled` in settings.json."""
        body: dict[str, Any] = {"iterations": iterations}
        body.update(kwargs)
        return self._post("/api/benchmark", body)

    def find_placement(
        self, prefab: str, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
        x: int | None = None, y: int | None = None, radius: int | None = None,
    ) -> dict[str, Any]:
        """Find valid placements for a building in an area. Returns spots sorted by path access."""
        body: dict[str, Any] = {"prefab": prefab, "x1": x1, "y1": y1, "x2": x2, "y2": y2}
        if x is not None and y is not None:
            body["x"] = x
            body["y"] = y
            if radius is not None:
                body["radius"] = radius
        return self._post("/api/placement/find", body)

    def place_building(
        self, prefab: str, x: int, y: int, z: int, orientation: str = "south",
    ) -> dict[str, Any]:
        """Place a building. Orientation: south, west, north, east."""
        return self._post("/api/building/place", {
            "prefab": prefab, "x": x, "y": y, "z": z,
            "orientation": str(orientation).lower(),
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
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z, "marked": True,
        })

    def plant_crop(self, x1: int, y1: int, x2: int, y2: int, z: int, crop: str) -> dict[str, Any]:
        """Mark area for planting. Crops: Kohlrabi, Cassava, Carrot, Potato, Wheat, etc."""
        return self._post("/api/planting/mark", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z, "crop": crop,
        })

    def find_planting(
        self, crop: str, id: int = 0, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, z: int = 0,
    ) -> dict[str, Any]:
        """Find valid planting spots. Use id for farmhouse range, or x1/y1/x2/y2/z for area."""
        return self._post("/api/planting/find", {
            "crop": crop, "id": id,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z,
        })

    def building_range(self, id: int) -> dict[str, Any]:
        """Get work range tiles for a building (farmhouse, lumberjack, forester)."""
        return self._post("/api/building/range", {"id": id})

    def clear_planting(self, x1: int, y1: int, x2: int, y2: int, z: int) -> dict[str, Any]:
        """Clear planting marks from a rectangular area."""
        return self._post("/api/planting/clear", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z,
        })

    def clear_trees(self, x1: int, y1: int, x2: int, y2: int, z: int) -> dict[str, Any]:
        """Clear tree cutting marks from a rectangular area."""
        return self._post("/api/cutting/area", {
            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "z": z, "marked": False,
        })

    def set_storage(self, id: int, good: str = "", mode: str = "") -> dict[str, Any]:
        """Set storage mode and/or allowed good. mode: accept, obtain, supply, empty. good: good name or 'none' to clear."""
        body: dict[str, int | str] = {"id": id}
        if good:
            body["good"] = good
        if mode:
            body["mode"] = mode
        return self._post("/api/building/storage", body)

    def place_path(
        self, x1: int, y1: int, x2: int, y2: int, _z: int = 0,
        style: str = "direct", sections: int = 0, timings: bool = False,
    ) -> dict[str, Any]:
        """Route a path using A* to avoid obstacles, with auto-stairs at z-level changes. z param ignored. style: 'direct' (staircase) or 'straight' (minimize turns). sections: 0=all, N=place N stair crossings then stop."""
        body: dict[str, Any] = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "style": style}
        if sections:
            body["sections"] = sections
        if timings:
            body["timings"] = True
        return self._post("/api/path/place", body)

    # ------------------------------------------------------------------
    # Automation
    # ------------------------------------------------------------------

    def link(self, source_id: int, target_id: int, input: str = "a") -> dict[str, Any]:
        """Wire a sensor/relay output to a building automation input. input: a, b, or reset (for Memory)."""
        return self._post("/api/automation/link", {
            "sourceId": source_id, "targetId": target_id, "input": input,
        })

    def unlink(self, id: int, input: str = "a") -> dict[str, Any]:
        """Disconnect an automation input. input: a, b, or reset (for Memory)."""
        return self._post("/api/automation/unlink", {"id": id, "input": input})

    def configure_automation(self, id: int, property: str, value: str) -> dict[str, Any]:
        """Configure an automation component property (threshold, mode, etc.)."""
        return self._post("/api/automation/configure", {
            "id": id, "property": property, "value": value,
        })

    def rename_automation(self, id: int, name: str) -> dict[str, Any]:
        """Set a custom label for an automation entity."""
        return self._post("/api/automation/rename", {"id": id, "name": name})

    # ------------------------------------------------------------------
    # Helpers (static, server-aggregated)
    # ------------------------------------------------------------------

    def tree_clusters(self) -> TreeClusters:
        """Find clusters of grown trees. Returns top clusters by grown count."""
        return TreeClusters.model_validate(self._get_json("/api/tree_clusters"))

    def food_clusters(self) -> FoodClusters:
        """Find clusters of gatherable food (berries, bushes). Returns top clusters by grown count."""
        return FoodClusters.model_validate(self._get_json("/api/food_clusters"))

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

    def map(self, x1: int, y1: int, x2: int, y2: int) -> str:
        """Colored ASCII map with terrain height shading, buildings, water, trees.

        Returns the rendered string; the CLI prints it. Library callers can do
        `print(client.map(...))` or feed the string into their own UI layer.
        """
        from tbot.formatters.map import render_map

        tiles_response = self._get_json("/api/tiles", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})
        return render_map(tiles_response, x1, y1, x2, y2)

    # ------------------------------------------------------------------
    # Settlement memory (delegates to SettlementContext)
    # ------------------------------------------------------------------

    def settlement_context(self) -> SettlementContext:
        """Return (lazily) the SettlementContext for the current game."""
        if self._ctx is None:
            try:
                r = self.s.get(f"{self.url}/api/settlement", timeout=5)
                name = r.json().get("name", "unknown")
            except Exception:
                name = "unknown"
            self._ctx = SettlementContext(name)
        return self._ctx

    def brain(self, goal: str | None = None) -> dict[str, Any]:
        """Live summary plus persistent goal/tasks/locations.

        Fetches `/api/summary`, refreshes `brain.toon` for the current settlement
        (auto-seeding well-known locations on first run), then returns a compact
        rendering suitable for AI-agent context windows.
        """
        summary = self._get_json("/api/summary")
        settlement_name = summary.get("settlement") or summary.get("settlementName") or "unknown"
        self._ctx = SettlementContext(settlement_name)
        brain_data = self._ctx.refresh_brain(summary, goal=goal)
        return {
            "summary": compact_summary(summary),
            "goal": brain_data["goal"],
            "tasks": brain_data["tasks"],
            "locations": compact_locations(brain_data["locations"]),
        }

    def set_location(
        self, name: str, x: int, y: int, z: int = 0, note: str = "",
    ) -> dict[str, Any]:
        """Save a named location. Persists across sessions."""
        return self.settlement_context().set_location(name, x, y, z, note)

    def remove_location(self, name: str) -> dict[str, Any]:
        """Remove a named location."""
        return self.settlement_context().remove_location(name)

    def list_locations(self) -> dict[str, Any]:
        """List all saved locations."""
        return self.settlement_context().list_locations()

    def clear_brain(self) -> dict[str, Any]:
        """Wipe memory for the current settlement. Run brain again to start fresh."""
        return self.settlement_context().clear()

    def add_task(self, action: str) -> dict[str, Any]:
        """Add a pending task to brain.toon. Returns the new task."""
        return self.settlement_context().add_task(action)

    def update_task(self, id: int, status: str, error: str | None = None) -> dict[str, Any]:
        """Update task status. status: pending/active/done/failed. Optional error for failed."""
        return self.settlement_context().update_task(id, status, error)

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all tasks from brain.toon."""
        return self.settlement_context().list_tasks()

    def clear_tasks(self, status: str = "done") -> dict[str, Any]:
        """Remove tasks with given status (default: done). Returns count cleared."""
        return self.settlement_context().clear_tasks(status)

    # ------------------------------------------------------------------
    # Agent control (server-side endpoints; will be reworked in PR 2)
    # ------------------------------------------------------------------

    def agent_status(self) -> dict[str, Any]:
        """Get AI agent loop status."""
        return self._get("/api/agent/status")

    def agent_stop(self) -> dict[str, Any]:
        """Stop AI agent loop."""
        return self._post("/api/agent/stop", {})

    def find(
        self, source: str, name: str | None = None,
        x: int | None = None, y: int | None = None, radius: int = 20, limit: int = 0,
    ) -> dict[str, Any]:
        """Find entities from a source (buildings/trees/gatherables/beavers). Filters server-side."""
        params: dict[str, int | str] = {"limit": limit}
        if name:
            params["name"] = name
        if x is not None and y is not None:
            params["x"] = x
            params["y"] = y
            params["radius"] = radius
        return self._get(f"/api/{source}", params=params)
