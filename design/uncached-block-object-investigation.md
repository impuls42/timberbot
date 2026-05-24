# Uncached Block Object Investigation

> **Status:** historical investigation. The fix landed in `TimberbotEntityRegistry`; this doc captures the root cause and reasoning.

## Summary

Timberbot pathing attempted to route through a visible ruin on the map. The game had a real blocking object at that location, but Timberbot's normal occupancy model did not surface it. The root cause is that Timberbot currently caches only buildings, living natural resources, and beavers/bots. Other world `BlockObject` entities can exist in the raw game entity registry while remaining invisible to `/api/tiles` and path planning.

This is not limited to one ruin prefab. Live inspection showed multiple uncached block-object groups in the current save, including `Ruins`, `MapEditorObjects`, `MapEditorWater`, and `Wood`.

## Exact Live Evidence

### Landmark used

A `LumberjackFlag` was placed one tile east of a visible ruin.

Live building data showed:

- `LumberjackFlag` at `(156,215,8)`

### What `/api/tiles` reported

Inspecting the area around the flag:

- tile `(155,215)` reported no occupants
- tile `(156,215)` reported the `LumberjackFlag`

That means Timberbot's normal tile feed treated the west-adjacent tile as empty.

### What raw debug reported

Debug inspection of the raw game entity registry found a ruin entity exactly on that "empty" tile:

- `RuinColumnH3(Clone)`
- entity registry index `1890`
- transform position `(155.00, 8.00, 215.00)`

This proves the game had a real world object at `(155,215,8)` even though `/api/tiles` did not show it.

## What Debug Proved

Raw debug over `Placement._entityService._entityRegistry.Entities` showed that ruin objects are real `EntityComponent` instances in the live game registry.

A sampled ruin entity exposed:

- `Name = UndergroundRuins(Clone)`
- a live `Timberborn.BlockSystem.BlockObject` component
- `PlaceableBlockObjectSpec.ToolGroupId = Ruins`
- `BlockObjectSpec.Overridable = False`

So these are real block objects with physical footprint and placement semantics. They are not fake scenery from Timberbot's point of view; Timberborn models them as placeable/blocking objects.

## Why Timberbot Missed Them

### Current cache categories

`TimberbotEntityRegistry` currently caches entities only when they match one of these categories:

- `Building`
- `LivingNaturalResource`
- `NeedManager`

That becomes:

- `Buildings`
- `NaturalResources`
- `Beavers`

### Current tile occupancy logic

`CollectTiles()` builds tile occupants from:

- `_cache.Buildings.Read`
- `_cache.NaturalResources.Read`

Only those two caches contribute occupant names to `/api/tiles`.

### Resulting gap

If an entity:

- has a real `BlockObject`
- occupies tiles in the world
- is **not** a `Building`
- is **not** a `LivingNaturalResource`
- is **not** a beaver/bot

then Timberbot currently does not surface it through its normal occupancy model.

That is exactly what happened with the ruin next to the placed `LumberjackFlag`.

## Why This Affects Pathing

Pathing and placement validation both rely on the same incomplete cache model.

### Surface graph / cost grid

In `TimberbotPlacement`, the path surface graph blocks tiles from:

- cached buildings
- cached natural resources

Uncached block objects are not included, so path planning can treat those tiles as traversable.

### Placement blocker naming

When placement validation detects a conflict, blocker names are currently resolved from:

- cached buildings
- cached natural resources

If the blocker is an uncached block object, the game may report a collision but Timberbot cannot name it cleanly and falls back to `unknown`.

## Live Census of Raw Block Objects

Grouping live raw entities by `PlaceableBlockObjectSpec.ToolGroupId` produced:

- `Paths`: `547`
- `Ruins`: `378`
- `MapEditorObjects`: `44`
- `MapEditorWater`: `34`
- `Wood`: `1`

This matters because it shows the issue is not a narrow `Ruin` name bug. There are multiple categories of uncached block objects in the world.

Within the `Ruins` group, the most common live names included:

- `RuinColumnH1(Clone)`: `100`
- `RuinColumnH2(Clone)`: `99`
- `RuinColumnH3(Clone)`: `96`
- `RuinColumnH4(Clone)`: `36`
- `RuinColumnH7(Clone)`: `18`
- `RuinColumnH5(Clone)`: `16`
- `RuinColumnH6(Clone)`: `5`
- `RuinColumnH8(Clone)`: `4`
- `UndergroundRuins(Clone)`: `4`

## Root Cause

The bug is a category gap between:

- the full game entity registry
- Timberbot's reduced cached world model

Timberbot currently assumes that the physically relevant world blockers are covered by buildings and natural resources. That assumption is false for this save, and likely false in general for maps with ruins and editor-placed objects.

## Recommended Fix Direction

The correct fix is broader than a `Ruin` name special-case.

Timberbot should add coverage for uncached non-`Paths` block objects by:

- discovering raw entities with `BlockObject` and `PlaceableBlockObjectSpec`
- excluding normal cached categories already handled elsewhere
- excluding `ToolGroupId == Paths`
- caching their occupied tiles and names
- exposing them through `/api/tiles` and debug tools
- treating them as blockers in path planning and placement diagnostics

This should cover:

- `Ruins`
- `MapEditorObjects`
- `MapEditorWater`
- `Wood`

and similar future categories without depending on prefab-name matching.

## Reproduction Notes

The investigation used these live checks:

### 1. Find the landmark flag

Query live buildings and locate the placed `LumberjackFlag`.

Expected result:

- `LumberjackFlag` at `(156,215,8)`

### 2. Inspect the surrounding tiles

Inspect `(154..157,214..216)` with `/api/tiles`.

Expected result:

- `(155,215)` appears empty in normal tile occupants
- `(156,215)` shows the flag

### 3. Inspect raw entities via debug

Walk `Placement._entityService._entityRegistry.Entities` and filter names matching `Ruin` or `Relic`.

Expected result:

- a ruin entity exists at `(155.00, 8.00, 215.00)`

### 4. Group raw block objects by tool group

For each raw entity, inspect `~PlaceableBlockObjectSpec.ToolGroupId` and count by group.

Expected result:

- non-`Paths` groups exist live and are numerous

## Conclusion

Timberbot did detect the visible world incorrectly, not the user. The ruin was real. The bug is that Timberbot's cached occupancy model did not include that class of object, so pathing planned through incomplete obstacle data.

The durable fix is to cover uncached non-`Paths` block objects as first-class blockers in tiles/debug/pathing instead of handling only buildings and natural resources.
