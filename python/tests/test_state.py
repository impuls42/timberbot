"""Unit tests for timberbot.state."""
from __future__ import annotations

from timberbot.state import SettlementContext, compact_locations, compact_summary


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


def test_compact_summary_flattens_time_and_weather():
    s = {
        "faction": "Folktails",
        "time": {"dayNumber": 7, "dayProgress": 0.42, "speed": 2},
        "weather": {
            "cycle": 1, "cycleDay": 3, "isHazardous": True,
            "temperateWeatherDuration": 10, "hazardousWeatherDuration": 4,
        },
    }
    out = compact_summary(s)
    assert out["day"] == 7
    assert out["dayProgress"] == 0.42
    assert out["speed"] == 2
    assert "DROUGHT" in out["weather"]
    assert "time" not in out  # flattened away


def test_compact_summary_collapses_districts():
    s = {
        "districts": [{
            "name": "main",
            "population": {"adults": 10, "children": 4, "bots": 1},
            "resources": {"Water": 200, "Log": 50},
            "housing": {"occupiedBeds": 14, "totalBeds": 20, "homeless": 0},
            "employment": {"assigned": 12, "vacancies": 14, "unemployed": 2},
            "wellbeing": {"average": 18, "miserable": 0, "critical": 0},
        }],
    }
    out = compact_summary(s)
    d = out["districts"][0]
    assert d["pop"] == "10a 4c 1b"
    assert "Water:200" in d["resources"]
    assert d["beds"] == "14/20 homeless:0"


def test_compact_locations_renders_coords_species_note():
    locs = {
        "dc": {"x": 100, "y": 200, "z": 3},
        "forest": {"x": 50, "y": 60, "z": 1, "species": ["Pine", "Birch"]},
        "marked": {"x": 1, "y": 2, "note": "demolish later"},
    }
    out = compact_locations(locs)
    assert out["dc"] == "100,200,z3"
    assert out["forest"] == "50,60,z1 Pine,Birch"
    assert out["marked"] == "1,2,z0 demolish later"


# ---------------------------------------------------------------------------
# Migration from legacy <mod_dir>/memory/<settlement>/brain.toon to the new
# user-data-dir location (impuls42/timberbot#43 PR 3).
# ---------------------------------------------------------------------------

import warnings

import pytest


@pytest.fixture
def migration_paths(tmp_path, monkeypatch):
    """Two controlled paths: a legacy mod tree and the new data dir.

    Returns (legacy_brain_path, new_base, settlement).
    """
    settlement = "Castle"
    legacy_root = tmp_path / "legacy-docs"
    legacy_root.mkdir()
    # paths.memory_base() = mod_dir() / "memory" = TBOT_DOCUMENTS_DIR / Mods/Timberbot/memory
    legacy_dir = legacy_root / "Mods" / "Timberbot" / "memory" / settlement
    legacy_brain = legacy_dir / "brain.toon"
    monkeypatch.setenv("TBOT_DOCUMENTS_DIR", str(legacy_root))

    new_root = tmp_path / "new-data"
    monkeypatch.setenv("TBOT_DATA_DIR", str(new_root))

    # `paths.documents_dir()` caches its first resolution per-process, so
    # tests that touched it earlier in the same session would lock in the
    # conftest TBOT_DOCUMENTS_DIR. Clear the cache so our override takes.
    from timberbot import paths
    paths.reset_cache()

    return legacy_brain, new_root / "memory", settlement


def test_migration_copies_legacy_brain(migration_paths):
    legacy_brain, new_base, settlement = migration_paths
    legacy_brain.parent.mkdir(parents=True, exist_ok=True)
    legacy_brain.write_text("toon:legacy-marker\n")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        ctx = SettlementContext(settlement)

    new_brain = new_base / settlement / "brain.toon"
    assert new_brain.is_file()
    assert new_brain.read_text() == "toon:legacy-marker\n"
    # legacy file is left in place for user-side verification
    assert legacy_brain.is_file()
    # exactly one migration warning, mentioning both paths
    matches = [w for w in caught if "migrated brain.toon" in str(w.message)]
    assert len(matches) == 1
    msg = str(matches[0].message)
    assert str(legacy_brain) in msg and str(new_brain) in msg
    # idempotent: a second context for the same settlement is a no-op
    with warnings.catch_warnings(record=True) as caught2:
        warnings.simplefilter("always", UserWarning)
        SettlementContext(settlement)
    assert not any("migrated brain.toon" in str(w.message) for w in caught2)


def test_migration_skipped_when_new_already_exists(migration_paths):
    legacy_brain, new_base, settlement = migration_paths
    legacy_brain.parent.mkdir(parents=True, exist_ok=True)
    legacy_brain.write_text("toon:legacy-marker\n")
    new_brain = new_base / settlement / "brain.toon"
    new_brain.parent.mkdir(parents=True, exist_ok=True)
    new_brain.write_text("toon:new-wins\n")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        SettlementContext(settlement)

    # new content untouched, no warning emitted
    assert new_brain.read_text() == "toon:new-wins\n"
    assert not any("migrated brain.toon" in str(w.message) for w in caught)


def test_no_migration_when_no_legacy_file(migration_paths):
    legacy_brain, new_base, settlement = migration_paths
    new_brain = new_base / settlement / "brain.toon"
    assert not legacy_brain.exists()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        ctx = SettlementContext(settlement)

    assert ctx.load_brain() == {}
    assert not new_brain.exists()
    assert not any("migrated brain.toon" in str(w.message) for w in caught)
