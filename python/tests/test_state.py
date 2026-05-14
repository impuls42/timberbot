"""Unit tests for tbot.state.SettlementContext."""
from __future__ import annotations

from tbot.state import SettlementContext


def test_load_brain_returns_empty_when_no_file(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    assert ctx.load_brain() == {}


def test_save_and_load_round_trip(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    ctx.save_brain({"goal": "build a dam", "tasks": []})
    loaded = ctx.load_brain()
    assert loaded["goal"] == "build a dam"


def test_set_and_remove_location(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    saved = ctx.set_location("dc", 110, 130, 2, "main district center")
    assert saved == {"saved": "dc", "x": 110, "y": 130, "z": 2}
    assert ctx.list_locations() == {
        "dc": {"x": 110, "y": 130, "z": 2, "note": "main district center"},
    }
    removed = ctx.remove_location("dc")
    assert removed == {"removed": "dc"}
    assert ctx.list_locations() == {}


def test_remove_unknown_location_returns_error(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    out = ctx.remove_location("nonexistent")
    assert out["error"] == "not_found"
    assert out["available"] == []


def test_task_lifecycle(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    t1 = ctx.add_task("plant trees")
    t2 = ctx.add_task("build dam")
    assert t1["id"] == 1 and t1["status"] == "pending"
    assert t2["id"] == 2

    ctx.update_task(1, "done")
    cleared = ctx.clear_tasks(status="done")
    assert cleared == {"cleared": 1, "remaining": 1}
    assert [t["id"] for t in ctx.list_tasks()] == [2]


def test_update_task_with_error(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    ctx.add_task("place bridge")
    updated = ctx.update_task(1, "failed", error="no path")
    assert updated["error"] == "no path"


def test_settlement_name_is_sanitized(tmp_path):
    ctx = SettlementContext("My/Castle", base=tmp_path)
    assert ctx.settlement == "My_Castle"
    assert ctx.memory_dir == tmp_path / "My_Castle"


def test_clear_removes_settlement_dir(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    ctx.save_brain({"goal": "x"})
    assert ctx.memory_dir.exists()
    out = ctx.clear()
    assert "cleared" in out
    assert not ctx.memory_dir.exists()


def test_refresh_brain_seeds_from_summary(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    summary = {
        "districts": [{"dc": {"x": 100, "y": 200, "z": 3}}],
        "treeClusters": [{"x": 50, "y": 60, "z": 2, "species": {"Pine": 1}}],
        "foodClusters": [{"x": 70, "y": 80, "z": 1, "species": {"Bush": 5}}],
    }
    brain = ctx.refresh_brain(summary, goal="reach 50 beavers")
    assert brain["goal"] == "reach 50 beavers"
    assert brain["locations"]["dc"] == {"x": 100, "y": 200, "z": 3}
    assert brain["locations"]["forest"]["x"] == 50
    assert brain["locations"]["berries"]["species"] == ["Bush"]


def test_refresh_brain_keeps_existing_goal_when_none_passed(tmp_path):
    ctx = SettlementContext("Castle", base=tmp_path)
    ctx.save_brain({"goal": "old goal", "tasks": [], "locations": {"home": {"x": 1, "y": 2}}})
    brain = ctx.refresh_brain({"districts": []}, goal=None)
    assert brain["goal"] == "old goal"
    assert brain["locations"] == {"home": {"x": 1, "y": 2}}
