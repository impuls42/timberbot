"""Game MCP server: TimberbotClient wrapped as fastmcp tools with event envelopes.

Every tool takes a `cursor` parameter (the agent's current event position) and
returns an EventEnvelope — the tool result plus a meta block with game events
accumulated since that cursor. The agent scans meta.events before deciding its
next move and uses meta.advisory to gauge urgency.

The `observe` tool returns an empty result and is used to catch up on events
without taking any in-game action (useful at the start of a turn or after a
thinking pause).

Usage::

    from timberbot.api.client import TimberbotClient
    from timberbot.game_mcp import EventBus, create_mcp_server

    client = TimberbotClient()
    bus = EventBus()
    mcp = create_mcp_server(client, bus)
    await mcp.run_http_async(transport="sse", host="127.0.0.1", port=8091)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import fastmcp
from pydantic import BaseModel

from timberbot.api.client import TimberbotClient
from timberbot.game_mcp.bus import EventBus
from timberbot.game_mcp.models import Cursor, EventMeta

log = logging.getLogger("timberbot.game_mcp.server")


# ---------------------------------------------------------------------------
# Internal envelope builder
# ---------------------------------------------------------------------------


def _make_envelope(bus: EventBus, cursor: int, result: Any) -> dict[str, Any]:
    """Build the standard EventEnvelope dict for a tool response."""
    events, hw, truncated, dropped = bus.events_since(cursor)
    adv = bus.advisory(events)
    return {
        "result": result.model_dump(mode="json") if isinstance(result, BaseModel) else result,
        "meta": EventMeta(
            cursor=Cursor(consumed=cursor, high_water=hw),
            events=events,
            events_truncated=truncated,
            events_dropped=dropped,
            advisory=adv,
            hint=bus.hint(adv),
        ).model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_mcp_server(client: TimberbotClient, bus: EventBus) -> fastmcp.FastMCP:
    """Create and return a configured FastMCP instance with all game tools.

    The returned server is not yet running — call run_http_async() on it.
    All tools are async: they offload the blocking HTTP call to a thread pool
    executor while accessing the EventBus on the event loop.
    """
    mcp = fastmcp.FastMCP(
        "timberbot-game",
        instructions=(
            "Game tools for Timberborn. Every tool returns {result, meta}. "
            "Scan meta.events before deciding next action. "
            "Respect meta.advisory: normal=proceed, attention=re-evaluate, "
            "urgent=stop and address, halt=acknowledge and end session. "
            "Pass your current meta.cursor.high_water as the `cursor` param "
            "on the next call so you only receive new events."
        ),
    )

    loop_getter = asyncio.get_running_loop

    # ------------------------------------------------------------------
    # Observation tool (no game-side effect)
    # ------------------------------------------------------------------

    @mcp.tool
    async def observe(cursor: int = 0) -> dict[str, Any]:
        """Sync game events without taking any in-game action.

        Use at the start of a turn or after a thinking pause to catch up on
        world changes before deciding your next move.
        """
        return _make_envelope(bus, cursor, {})

    # ------------------------------------------------------------------
    # Read tools — query game state
    # ------------------------------------------------------------------

    @mcp.tool
    async def summary(cursor: int = 0) -> dict[str, Any]:
        """Colony overview: population, key resources, time, weather."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.summary)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def time(cursor: int = 0) -> dict[str, Any]:
        """Current game time: cycle, day, season, daytime flag."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.time)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def weather(cursor: int = 0) -> dict[str, Any]:
        """Current weather: temperature, drought/badtide status, forecast."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.weather)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def population(cursor: int = 0) -> dict[str, Any]:
        """Population counts: adults, children, homeless, total."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.population)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def resources(cursor: int = 0) -> dict[str, Any]:
        """All resource stocks: [{good, stock, capacity}]."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.resources)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def districts(cursor: int = 0) -> dict[str, Any]:
        """All districts: [{name, population, ...}]."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.districts)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def buildings(
        cursor: int = 0,
        limit: int = 0,
        offset: int = 0,
        detail: str = "basic",
        id: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> dict[str, Any]:
        """Buildings list. detail: basic|full. id: single building. name: substring filter. x/y/radius: proximity."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.buildings(
                limit=limit, offset=offset, detail=detail,
                id=id, name=name, x=x, y=y, radius=radius,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def trees(
        cursor: int = 0,
        limit: int = 0,
        offset: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> dict[str, Any]:
        """Trees: [{id, name, x, y, z, marked, alive, grown, growth}]. name: species filter."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.trees(
                limit=limit, offset=offset, name=name, x=x, y=y, radius=radius,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def crops(
        cursor: int = 0,
        limit: int = 0,
        offset: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> dict[str, Any]:
        """Planted crops: [{id, name, x, y, z, marked, alive, grown, growth}]."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.crops(
                limit=limit, offset=offset, name=name, x=x, y=y, radius=radius,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def gatherables(
        cursor: int = 0,
        limit: int = 0,
        offset: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> dict[str, Any]:
        """Gatherable resources (berry bushes, etc.): [{id, name, x, y, z, alive}]."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.gatherables(
                limit=limit, offset=offset, name=name, x=x, y=y, radius=radius,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def beavers(
        cursor: int = 0,
        limit: int = 0,
        offset: int = 0,
        detail: str = "basic",
        id: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> dict[str, Any]:
        """Beavers with wellbeing and needs. detail:full for all needs."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.beavers(
                limit=limit, offset=offset, detail=detail,
                id=id, name=name, x=x, y=y, radius=radius,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def workhours(cursor: int = 0) -> dict[str, Any]:
        """Work schedule: {endHours, areWorkingHours, hoursPassedToday}."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.workhours)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def science(cursor: int = 0) -> dict[str, Any]:
        """Science points and unlockable buildings: {points, unlockables}."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.science)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def wellbeing(cursor: int = 0) -> dict[str, Any]:
        """Population wellbeing by category: {beavers, categories}."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.wellbeing)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def notifications(cursor: int = 0) -> dict[str, Any]:
        """Game notification history."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.notifications)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def alerts(cursor: int = 0) -> dict[str, Any]:
        """Active alerts: unstaffed, unpowered, unreachable buildings."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.alerts)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def distribution(cursor: int = 0) -> dict[str, Any]:
        """Distribution settings per district."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.distribution)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def prefabs(cursor: int = 0) -> dict[str, Any]:
        """Available building templates: [{name, sizeX, sizeY, sizeZ}]."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.prefabs)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def power(cursor: int = 0) -> dict[str, Any]:
        """Power networks: [{id, supply, demand, buildings}]."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.power)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def speed(cursor: int = 0) -> dict[str, Any]:
        """Current game speed: {speed: 0-3}."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.speed)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def tree_clusters(cursor: int = 0) -> dict[str, Any]:
        """Top clusters of grown trees, sorted by count."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.tree_clusters)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def food_clusters(cursor: int = 0) -> dict[str, Any]:
        """Food-producing area clusters."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.food_clusters)
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def find_placement(
        cursor: int = 0,
        prefab: str = "",
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        x: int = 0,
        y: int = 0,
        radius: int = 0,
    ) -> dict[str, Any]:
        """Find valid placements for a building in an area. Returns spots sorted by path access."""
        loop = loop_getter()
        kw: dict[str, Any] = {"prefab": prefab, "x1": x1, "y1": y1, "x2": x2, "y2": y2}
        if x and y:
            kw["x"] = x
            kw["y"] = y
            if radius:
                kw["radius"] = radius
        result = await loop.run_in_executor(None, lambda: client.find_placement(**kw))
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def find_planting(
        cursor: int = 0,
        crop: str = "",
        id: int = 0,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        z: int = 0,
    ) -> dict[str, Any]:
        """Find valid planting spots. Use id for farmhouse range, or x1/y1/x2/y2/z for area."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.find_planting(
                crop=crop, id=id, x1=x1, y1=y1, x2=x2, y2=y2, z=z,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def building_range(cursor: int = 0, id: int = 0) -> dict[str, Any]:
        """Get work range tiles for a building (farmhouse, lumberjack, forester)."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, lambda: client.building_range(id=id))
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def brain(cursor: int = 0, goal: str = "") -> dict[str, Any]:
        """Agent memory: persistent tasks, named locations, colony state snapshot."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.brain(goal=goal or None)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def list_locations(cursor: int = 0) -> dict[str, Any]:
        """Named locations stored in agent memory: {name: {x, y, z, note}}."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, client.list_locations)
        return _make_envelope(bus, cursor, result)

    # ------------------------------------------------------------------
    # Write tools — mutate game state
    # ------------------------------------------------------------------

    @mcp.tool
    async def set_speed(cursor: int = 0, speed: int = 1) -> dict[str, Any]:
        """Set game speed. 0=pause, 1=normal, 2=fast, 3=fastest."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, lambda: client.set_speed(speed))
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def pause_building(cursor: int = 0, id: int = 0) -> dict[str, Any]:
        """Pause a building. Get id from buildings()."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, lambda: client.pause_building(id))
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def unpause_building(cursor: int = 0, id: int = 0) -> dict[str, Any]:
        """Resume a paused building."""
        loop = loop_getter()
        result = await loop.run_in_executor(None, lambda: client.unpause_building(id))
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_priority(
        cursor: int = 0, id: int = 0, priority: str = "Normal", type: str = "",
    ) -> dict[str, Any]:
        """Set building priority. priority: VeryLow, Normal, VeryHigh. type: workplace or construction."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_priority(id=id, priority=priority, type=type)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_haul_priority(
        cursor: int = 0, id: int = 0, prioritized: bool = True,
    ) -> dict[str, Any]:
        """Set hauler priority on a building. Haulers deliver goods here first when True."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_haul_priority(id=id, prioritized=prioritized)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_recipe(cursor: int = 0, id: int = 0, recipe: str = "") -> dict[str, Any]:
        """Set manufactory recipe. Use 'none' to clear. Lists available recipes on error."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_recipe(id=id, recipe=recipe)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_farmhouse_action(
        cursor: int = 0, id: int = 0, action: str = "planting",
    ) -> dict[str, Any]:
        """Set farmhouse priority action: 'planting' or 'harvesting'."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_farmhouse_action(id=id, action=action)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_plantable_priority(
        cursor: int = 0, id: int = 0, plantable: str = "",
    ) -> dict[str, Any]:
        """Set prioritized plantable on forester/gatherer. Use 'none' to clear."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_plantable_priority(id=id, plantable=plantable)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_workers(cursor: int = 0, id: int = 0, count: int = 1) -> dict[str, Any]:
        """Set desired worker count for a building (0 to maxWorkers)."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_workers(id=id, count=count)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_floodgate(
        cursor: int = 0, id: int = 0, height: float = 0.0,
    ) -> dict[str, Any]:
        """Set floodgate open height (clamped to min/max for that gate)."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_floodgate(id=id, height=height)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_workhours(cursor: int = 0, end_hours: int = 18) -> dict[str, Any]:
        """Set when work ends (1-24). Beavers work from dawn until end_hours."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_workhours(end_hours=end_hours)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_distribution(
        cursor: int = 0,
        district: str = "",
        good: str = "",
        import_option: str = "",
        export_threshold: int = -1,
    ) -> dict[str, Any]:
        """Set import/export for a good in a district. import_option: Auto or Forced (empty = leave unchanged)."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_distribution(
                district=district, good=good,
                import_option=import_option, export_threshold=export_threshold,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_storage(
        cursor: int = 0, id: int = 0, good: str = "", mode: str = "",
    ) -> dict[str, Any]:
        """Set storage mode and/or allowed good. mode: accept, obtain, supply, empty."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_storage(id=id, good=good, mode=mode)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def set_clutch(
        cursor: int = 0, id: int = 0, engaged: bool = True,
    ) -> dict[str, Any]:
        """Engage or disengage a mechanical clutch."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_clutch(id=id, engaged=engaged)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def unlock_building(cursor: int = 0, building: str = "") -> dict[str, Any]:
        """Unlock a building using science points."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.unlock_building(building=building)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def migrate(
        cursor: int = 0,
        from_district: str = "",
        to_district: str = "",
        count: int = 1,
    ) -> dict[str, Any]:
        """Move beavers between districts."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.migrate(
                from_district=from_district, to_district=to_district, count=count,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def place_building(
        cursor: int = 0,
        prefab: str = "",
        x: int = 0,
        y: int = 0,
        z: int = 0,
        orientation: str = "south",
    ) -> dict[str, Any]:
        """Place a building. orientation: south, west, north, east."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.place_building(
                prefab=prefab, x=x, y=y, z=z, orientation=orientation,
            )
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def demolish_building(cursor: int = 0, id: int = 0) -> dict[str, Any]:
        """Demolish a building. Get id from buildings()."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.demolish_building(id=id)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def demolish_crop(cursor: int = 0, id: int = 0) -> dict[str, Any]:
        """Demolish a planted crop entity. Get id from crops()."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.demolish_crop(id=id)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def mark_trees(
        cursor: int = 0,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        z: int = 0,
    ) -> dict[str, Any]:
        """Mark a rectangular area for tree cutting."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.mark_trees(x1=x1, y1=y1, x2=x2, y2=y2, z=z)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def clear_trees(
        cursor: int = 0,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        z: int = 0,
    ) -> dict[str, Any]:
        """Clear tree-cutting marks from a rectangular area."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.clear_trees(x1=x1, y1=y1, x2=x2, y2=y2, z=z)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def plant_crop(
        cursor: int = 0,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        z: int = 0,
        crop: str = "",
    ) -> dict[str, Any]:
        """Mark area for planting. Crops: Kohlrabi, Cassava, Carrot, Potato, Wheat, etc."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.plant_crop(x1=x1, y1=y1, x2=x2, y2=y2, z=z, crop=crop)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def clear_planting(
        cursor: int = 0,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        z: int = 0,
    ) -> dict[str, Any]:
        """Clear planting marks from a rectangular area."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.clear_planting(x1=x1, y1=y1, x2=x2, y2=y2, z=z)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def place_path(
        cursor: int = 0,
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
        style: str = "direct",
        sections: int = 0,
    ) -> dict[str, Any]:
        """Route a path via A*. style: 'direct' (staircase) or 'straight' (minimize turns)."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.place_path(
                x1=x1, y1=y1, x2=x2, y2=y2, style=style, sections=sections,
            )
        )
        return _make_envelope(bus, cursor, result)

    # ------------------------------------------------------------------
    # Automation
    # ------------------------------------------------------------------

    @mcp.tool
    async def link(
        cursor: int = 0,
        source_id: int = 0,
        target_id: int = 0,
        input: str = "a",
    ) -> dict[str, Any]:
        """Wire a sensor/relay output to a building automation input. input: a, b, or reset."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.link(source_id=source_id, target_id=target_id, input=input)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def unlink(cursor: int = 0, id: int = 0, input: str = "a") -> dict[str, Any]:
        """Disconnect an automation input. input: a, b, or reset."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.unlink(id=id, input=input)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def configure_automation(
        cursor: int = 0, id: int = 0, property: str = "", value: str = "",
    ) -> dict[str, Any]:
        """Configure an automation component property (threshold, mode, etc.)."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.configure_automation(id=id, property=property, value=value)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def rename_automation(
        cursor: int = 0, id: int = 0, name: str = "",
    ) -> dict[str, Any]:
        """Set a custom label for an automation entity."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.rename_automation(id=id, name=name)
        )
        return _make_envelope(bus, cursor, result)

    # ------------------------------------------------------------------
    # Memory tools (agent brain)
    # ------------------------------------------------------------------

    @mcp.tool
    async def set_location(
        cursor: int = 0,
        name: str = "",
        x: int = 0,
        y: int = 0,
        z: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        """Save a named location to agent memory."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.set_location(name=name, x=x, y=y, z=z, note=note or None)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def remove_location(cursor: int = 0, name: str = "") -> dict[str, Any]:
        """Remove a named location from agent memory."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.remove_location(name=name)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def add_task(cursor: int = 0, action: str = "") -> dict[str, Any]:
        """Add a task to agent memory."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.add_task(action=action)
        )
        return _make_envelope(bus, cursor, result)

    @mcp.tool
    async def update_task(
        cursor: int = 0,
        id: int = 0,
        status: str = "done",
        error: str = "",
    ) -> dict[str, Any]:
        """Update a task status: pending, active, done, failed."""
        loop = loop_getter()
        result = await loop.run_in_executor(
            None, lambda: client.update_task(id=id, status=status, error=error or None)
        )
        return _make_envelope(bus, cursor, result)

    return mcp
