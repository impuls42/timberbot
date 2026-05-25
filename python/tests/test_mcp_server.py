"""Unit tests for game_mcp.server — MCP tool registration and envelope shape."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp", reason="fastmcp (serve extra) not installed")

from timberbot.api.client import TimberbotClient
from timberbot.api.models._generated import (
    BuildingList,
    Summary,
    Time,
    Weather,
)
from timberbot.game_mcp.bus import EventBus
from timberbot.game_mcp.models import GameEvent, Severity
from timberbot.game_mcp.server import create_mcp_server


@pytest.fixture
def client() -> MagicMock:
    m = MagicMock(spec=TimberbotClient)
    # Use plain dicts as stub return values — avoids RootModel construction issues
    _ok = {"ok": True}
    m.summary.return_value = Summary.model_construct()
    m.time.return_value = Time.model_construct()
    m.weather.return_value = Weather.model_construct()
    m.population.return_value = {"adults": 0, "children": 0}
    m.resources.return_value = {"items": []}
    m.districts.return_value = {"districts": []}
    m.buildings.return_value = BuildingList.model_construct()
    m.trees.return_value = {"trees": []}
    m.crops.return_value = {"crops": []}
    m.gatherables.return_value = {"gatherables": []}
    m.beavers.return_value = {"beavers": []}
    m.workhours.return_value = {"endHours": 18}
    m.science.return_value = {"points": 0, "unlockables": []}
    m.wellbeing.return_value = {"categories": []}
    m.notifications.return_value = {"notifications": []}
    m.alerts.return_value = {"alerts": []}
    m.distribution.return_value = {"districts": []}
    m.prefabs.return_value = {"prefabs": []}
    m.power.return_value = {"networks": []}
    m.speed.return_value = {"speed": 1}
    m.tree_clusters.return_value = {"clusters": []}
    m.food_clusters.return_value = {"clusters": []}
    m.find_placement.return_value = {"spots": []}
    m.find_planting.return_value = {"spots": []}
    m.building_range.return_value = {"tiles": []}
    m.brain.return_value = {"goal": "", "tasks": [], "locations": {}}
    m.list_locations.return_value = {}
    # Write methods
    m.set_speed.return_value = _ok
    m.pause_building.return_value = _ok
    m.unpause_building.return_value = _ok
    m.set_priority.return_value = _ok
    m.set_haul_priority.return_value = _ok
    m.set_recipe.return_value = _ok
    m.set_farmhouse_action.return_value = _ok
    m.set_plantable_priority.return_value = _ok
    m.set_workers.return_value = _ok
    m.set_floodgate.return_value = _ok
    m.set_workhours.return_value = _ok
    m.set_distribution.return_value = _ok
    m.set_storage.return_value = _ok
    m.set_clutch.return_value = _ok
    m.unlock_building.return_value = _ok
    m.migrate.return_value = _ok
    m.place_building.return_value = {"id": 42}
    m.demolish_building.return_value = _ok
    m.demolish_crop.return_value = _ok
    m.mark_trees.return_value = _ok
    m.clear_trees.return_value = _ok
    m.plant_crop.return_value = _ok
    m.clear_planting.return_value = _ok
    m.place_path.return_value = {"placed": 5}
    m.link.return_value = _ok
    m.unlink.return_value = _ok
    m.configure_automation.return_value = _ok
    m.rename_automation.return_value = _ok
    m.set_location.return_value = _ok
    m.remove_location.return_value = _ok
    m.add_task.return_value = {"id": 1}
    m.update_task.return_value = _ok
    m.complain.return_value = {"id": 1, "category": "bug", "severity": "medium", "message": "test", "resolved": False}
    m.agent_message.return_value = _ok
    return m


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mcp(client, bus):
    return create_mcp_server(client, bus)


def _parse(result) -> dict:
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_expected_tools_registered(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "observe", "summary", "time", "weather", "population", "resources",
        "districts", "buildings", "trees", "crops", "gatherables", "beavers",
        "workhours", "science", "wellbeing", "notifications", "alerts",
        "distribution", "prefabs", "power", "speed", "tree_clusters",
        "food_clusters", "find_placement", "find_planting", "building_range",
        "brain", "list_locations",
        "set_speed", "pause_building", "unpause_building", "set_priority",
        "set_haul_priority", "set_recipe", "set_farmhouse_action",
        "set_plantable_priority", "set_workers", "set_floodgate", "set_workhours",
        "set_distribution", "set_storage", "set_clutch", "unlock_building",
        "migrate", "place_building", "demolish_building", "demolish_crop",
        "mark_trees", "clear_trees", "plant_crop", "clear_planting", "place_path",
        "link", "unlink", "configure_automation", "rename_automation",
        "set_location", "remove_location", "add_task", "update_task", "complain",
    }
    assert expected.issubset(names), f"Missing tools: {expected - names}"


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_returns_empty_result(mcp):
    r = await mcp.call_tool("observe", {"cursor": 0})
    data = _parse(r)
    assert data["result"] == {}
    assert "meta" in data


@pytest.mark.asyncio
async def test_envelope_has_required_meta_keys(mcp):
    r = await mcp.call_tool("summary", {"cursor": 0})
    data = _parse(r)
    assert "result" in data
    meta = data["meta"]
    for key in ("cursor", "events", "events_truncated", "events_dropped", "advisory"):
        assert key in meta, f"meta missing key: {key}"
    cursor = meta["cursor"]
    assert "consumed" in cursor
    assert "high_water" in cursor


@pytest.mark.asyncio
async def test_no_events_gives_normal_advisory(mcp):
    r = await mcp.call_tool("observe", {"cursor": 0})
    data = _parse(r)
    assert data["meta"]["advisory"] == "normal"
    assert data["meta"]["hint"] is None
    assert data["meta"]["events"] == []


@pytest.mark.asyncio
async def test_critical_event_gives_halt_advisory(mcp, bus):
    e = GameEvent(
        seq=0, type="building.collapsed", day=1,
        timestamp=int(time.time()), severity=Severity.critical,
    )
    bus.push(e)
    r = await mcp.call_tool("observe", {"cursor": 0})
    data = _parse(r)
    assert data["meta"]["advisory"] == "halt"
    assert data["meta"]["hint"] is not None
    assert len(data["meta"]["events"]) == 1
    assert data["meta"]["events"][0]["type"] == "building.collapsed"


@pytest.mark.asyncio
async def test_cursor_advances_with_high_water(mcp, bus):
    for _ in range(3):
        e = GameEvent(seq=0, type="season.change", day=1, timestamp=int(time.time()), severity=Severity.info)
        bus.push(e)

    r1 = await mcp.call_tool("observe", {"cursor": 0})
    d1 = _parse(r1)
    hw = d1["meta"]["cursor"]["high_water"]
    assert hw == 3

    r2 = await mcp.call_tool("observe", {"cursor": hw})
    d2 = _parse(r2)
    assert d2["meta"]["events"] == []
    assert d2["meta"]["advisory"] == "normal"


@pytest.mark.asyncio
async def test_warn_event_gives_urgent_advisory(mcp, bus):
    e = GameEvent(seq=0, type="drought.start", day=1, timestamp=int(time.time()), severity=Severity.warn)
    bus.push(e)
    r = await mcp.call_tool("summary", {"cursor": 0})
    data = _parse(r)
    assert data["meta"]["advisory"] == "urgent"


@pytest.mark.asyncio
async def test_notice_event_gives_attention_advisory(mcp, bus):
    e = GameEvent(seq=0, type="beaver.died", day=1, timestamp=int(time.time()), severity=Severity.notice)
    bus.push(e)
    r = await mcp.call_tool("observe", {"cursor": 0})
    data = _parse(r)
    assert data["meta"]["advisory"] == "attention"


# ---------------------------------------------------------------------------
# Specific tool calls (verifies they hit client methods)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_calls_client(mcp, client):
    await mcp.call_tool("summary", {"cursor": 0})
    client.summary.assert_called_once()


@pytest.mark.asyncio
async def test_buildings_passes_filters(mcp, client):
    await mcp.call_tool("buildings", {"cursor": 0, "name": "Stockpile", "detail": "full"})
    client.buildings.assert_called_once_with(
        limit=0, offset=0, detail="full", id=0,
        name="Stockpile", x=0, y=0, radius=0,
    )


@pytest.mark.asyncio
async def test_place_building_returns_result(mcp, client):
    r = await mcp.call_tool("place_building", {"cursor": 0, "prefab": "WaterPump", "x": 10, "y": 20, "z": 1})
    data = _parse(r)
    assert data["result"] == {"id": 42}
    client.place_building.assert_called_once_with(
        prefab="WaterPump", x=10, y=20, z=1, orientation="south"
    )


@pytest.mark.asyncio
async def test_set_speed_calls_client(mcp, client):
    await mcp.call_tool("set_speed", {"cursor": 0, "speed": 2})
    client.set_speed.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_migrate_calls_client(mcp, client):
    await mcp.call_tool("migrate", {"cursor": 0, "from_district": "A", "to_district": "B", "count": 3})
    client.migrate.assert_called_once_with(from_district="A", to_district="B", count=3)


@pytest.mark.asyncio
async def test_complain_returns_envelope(mcp, client):
    r = await mcp.call_tool("complain", {
        "cursor": 0, "message": "buildings() is broken", "category": "bug", "severity": "high",
    })
    data = _parse(r)
    assert "result" in data and "meta" in data
    client.complain.assert_called_once_with(
        message="buildings() is broken", category="bug", severity="high",
    )


@pytest.mark.asyncio
async def test_complain_calls_on_complaint_callback(client, bus):
    called_with: list[tuple] = []

    async def capture(msg: str, cat: str, sev: str) -> None:
        called_with.append((msg, cat, sev))

    mcp_with_cb = create_mcp_server(client, bus, on_complaint=capture)
    await mcp_with_cb.call_tool("complain", {
        "cursor": 0, "message": "missing crop_yield", "category": "missing_feature", "severity": "low",
    })
    assert called_with == [("missing crop_yield", "missing_feature", "low")]


@pytest.mark.asyncio
async def test_complain_no_callback_does_not_crash(mcp, client):
    r = await mcp.call_tool("complain", {"cursor": 0, "message": "test", "category": "inconsistency"})
    data = _parse(r)
    assert "result" in data
