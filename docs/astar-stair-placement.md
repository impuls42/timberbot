# A* Path Building with Stair Placement. Current State

## What we're building

`place_path` routes a path from `(x1,y1)` to `(x2,y2)` across a Timberborn map. Timberborn pathing is 3D: routes may stay flat, cross a single z-change with one stair, or traverse multi-level height changes using a chain of `platforms + stairs`.

The end goal is:

- true A* correctness: cheapest route under the chosen cost model
- one search pass through a graph that already knows which 3D connectors are physically valid
- no post-hoc guessing of stair orientation
- correct in-game emission of paths, stairs, and platforms from the connector chosen by A*

If we want true A* optimality, every traversable edge must have a positive minimum cost that matches the heuristic's lower bound. Existing path/platform/stair reuse therefore needs to cost `1`, not `0`.

## How vertical movement works in Timberborn

- Stairs are 1x1 buildings placed on the lower tile of a 1z transition
- Stairs have a lower entrance side and a higher exit side
- Orientation determines which direction is uphill: `south(0)`, `west(1)`, `north(2)`, `east(3)`
- The entrance tile must have path connectivity on the lower side
- The exit tile must have path connectivity on the higher side
- Multi-level climbs are not one special building; they are a ramp chain made of `stairs + platforms`
- For multi-level climbs, each ramp tile may need zero or more platforms under a stair
- Chaining stairs is valid: the exit of one connector can lead into the entrance of the next

## Why the old diagonal fixes failed

### Failed fix 1: Lookback
Look back 5 waypoints to find a dominant direction.

Problem: the dominant direction did not match the actual path direction at the z-change tile, so the stair entrance drifted away from the chosen route and required detours.

### Failed fix 2: Destination-based direction
Orient stairs toward the destination.

Problem: destination direction is not enough to decide which lower tile the stair should occupy. It could choose the wrong terrain column and produce impossible placement.

## Current design direction

### Safe A* stays
We are keeping the new search core:

- heuristic is plain Manhattan distance
- score is exactly `f = g + h`
- style does not affect the numeric score
- style may only break ties among equal-`f` nodes
- blocked edges are excluded with `edgeCost >= 255`

This is the right foundation and should not be reverted.

### Graph-based 3D connectors stay
The correct abstraction is still:

- model vertical movement as directed graph connectors
- let A* choose among those connectors
- emit the exact connector chosen by the route

That is better than the older 3-pass approach where A* found a mostly 2D route first and the code invented stairs/platforms afterward.

## Cost model for real A*

Pathfinding minimizes total movement cost, not tile count.

- A node = one tile we can stand on
- An edge = one move from one tile to another
- Edge cost = the price of that move

Recommended costs:

- existing path/platform/stairs tile entry = `1`
- open ground = `2`
- shallow water = `8`
- deep water = `50`
- valid 1z stair connector edge = `20`
- valid multi-level connector edge = `20 * levels`
- impassable sentinel = `255`

This preserves the intended preference:

- reuse existing infrastructure first
- build new path when needed
- avoid water when possible
- avoid unnecessary elevation change

## Why `path = 0` breaks A*

If existing path edges cost `0`, Manhattan distance is no longer a safe lower bound.

Example:

- Goal is 5 tiles away
- Those 5 tiles are already existing path
- True remaining cost can be `0`
- Any positive Manhattan-based heuristic says `> 0`

That overestimates, which means the search is no longer guaranteed optimal.

## Why plain Manhattan works now

If the minimum traversable edge cost is `1`, then plain Manhattan distance is admissible:

- every remaining move costs at least `1`
- therefore `h = abs(gx - x) + abs(gy - y)` cannot overestimate

So safe A* should remain:

```csharp
int h0 = Math.Abs(gx - sx) + Math.Abs(gy - sy);
...
int h = Math.Abs(gx - nx) + Math.Abs(gy - ny);
int nf = tentG + h;
```

## Where the implementation is now

`TimberbotPlacement.cs` has been moved off the old tile-only hybrid and onto a surface-graph model.

Current shape of the code:

- walkable surfaces are explicit nodes keyed by `(x, y, z)`
- flat movement happens only between surfaces at the same `z`
- existing stairs are reusable directed connector edges in that same graph
- generated new stairs / ramps are also directed connector edges in that same graph
- `RoutePath()` replays the chosen edge sequence instead of inferring vertical movement from terrain-only heights after the search

This is the right long-term architecture because existing vertical reuse and generated vertical placement now share one traversal model.

## Current graph model

### Surface nodes

The graph currently creates nodes for:

- terrain walk surfaces
- existing path surfaces
- existing platform top surfaces

Entry cost policy in the code remains:

- existing path/platform surface entry = `1`
- terrain/open ground = `2`
- shallow water = `8`
- deep water = `50`
- blocked/overhang/resource/building = omitted from the graph

### Connector edges

The graph currently creates directed connector edges for:

- existing 1z stairs already built in the world
- generated 1z stairs
- generated multi-level ramp connectors

Connector edges carry:

- `BaseZ`
- `Levels`
- `OrientIdx`
- entrance tile and z
- exit tile and z
- `RequiresPlacement`
- ramp tile list with `platCount`

That means A* is now choosing between reusable vertical infrastructure and newly buildable vertical infrastructure inside one search space.

## Connector generation rules

### Existing vertical reuse

Existing stairs are modeled as reusable directed edges:

- lower entrance tile -> higher exit tile
- reverse edge for downhill traversal
- cost `20`
- `RequiresPlacement = false`

Existing platforms contribute reusable top walk surfaces at `z + 1`.

### Generated connectors

Generated connectors are still terrain-driven and follow the same basic rules:

- `1z`: stair on lower tile, entrance one tile behind, exit one tile ahead on the higher side
- `2z+`: ramp chain with one stair per ramp tile and `platCount` platforms underneath
- uphill `platCount = step`
- downhill uses the reverse directed edge over the same connector plan
- generated connector cost = `20 * levels`

### Validation before exposing a generated connector

A generated connector is traversable only if:

- every ramp tile is in bounds
- every ramp tile matches the expected base terrain z
- entrance tile is buildable at the lower z
- exit tile is buildable at the higher z
- blocking terrain/buildings/resources/overhangs do not invalidate the path

If any of those fail, the connector is omitted from the graph.

## Route emission rules

`RoutePath()` now emits from the chosen graph edges, not from terrain-height diffs.

- flat edge to a buildable terrain/path surface: place `Path` on the destination node if needed
- reusable existing flat/path/platform edge: place nothing
- reusable existing stair edge: place nothing
- generated connector edge: place platforms first, then stairs, then continue from the connector exit surface
- `sections` still counts connector crossings
- `stoppedAt` still uses the connector exit tile

Placement dedupe currently tracks separate keys for:

- paths
- platforms
- stairs

That avoids path/stair/platform collisions on the same `(x, y)` column.

## Current implementation status

### Implemented in code

- safe A* remains plain Manhattan with `f = g + h`
- the old `terrainHeights + walkHeights + injected connector` hybrid path model has been replaced in `TimberbotPlacement.cs`
- the new code uses a surface graph with explicit `SurfaceNode` and `GraphEdge` records
- existing vertical reuse is part of the graph now, not a later add-on
- route emission follows chosen graph edges rather than trying to rediscover connectors afterward

### Still not validated end-to-end

- I have not completed live validation against the running game after the surface-graph rewrite
- east/west/north/south geometry still needs in-game confirmation
- multi-level ramp geometry still needs in-game confirmation
- there may still be connector entrance/exit mismatches that only show up in live placement

### Current external blocker

- the project build is currently blocked by an unrelated compile error in `timberbot/src/TimberbotWrite.cs`
- that error is outside the pathing change and needs to be cleared before full compile-and-test validation can finish

## What needs to be finished

1. **Validate 1z generated placement in all four directions**
Run isolated one-case tests and direct live `place_path(...)` calls for east, west, north, and south.

2. **Validate existing vertical reuse in-game**
Confirm that routes can traverse already built stairs/platforms without trying to rebuild them or falling back to flat detours.

3. **Validate multi-level generated connectors**
Confirm that `2z+` routes place visible `platforms + stairs` with the expected geometry.

4. **Preserve safe A* rules while debugging**
Do not reintroduce weighted `f`, path cost `0`, or post-hoc stair guessing.

5. **Update tests around the real contract**
Keep single-case directional tests, and stop using `placed.paths` as a proxy for total traveled distance.

## Implementation checklist

- Keep `AStarPath()` as safe A*: plain Manhattan and `f = g + h`
- Keep existing path/platform reuse at cost `1`
- Keep existing stairs as reusable directed connector edges
- Keep generated stairs/ramps in the same `GraphEdge` model
- Keep route emission edge-driven
- Validate connector geometry in-game for 1z and 2z+
- Keep `sections` based on connector crossings
- Keep test cases isolated so path reuse from nearby tests does not poison results
## Key files

- `timberbot/src/TimberbotPlacement.cs`
  - `RoutePath()`. route emission and connector placement
  - `BuildCostGrid()`. connector generation and edge costs
  - `AStarPath()`. safe A* search core
- `python/tests/integration/` (legacy `test_validation.py` content)
  - 1z / 2z / diagonal / obstacle validation
- `python/src/timberbot/api/client.py`
  - client interface for `place_path()`

## Research sources

- [Unity multi-level pathfinding](https://forum.unity.com/threads/multi-level-height-pathfinding.373728/). model level connections as portal edges
- [Red Blob Games grid algorithms](https://www.redblobgames.com/pathfinding/grids/algorithms.html). edge-based costs and admissible heuristics
- [Red Blob Games A* introduction](https://www.redblobgames.com/pathfinding/a-star/introduction.html). `g`, `h`, `f`, and lower-bound heuristics
- [A* 3D grid pathfinding](https://answers.unity.com/questions/1096235/using-stairs-in-3d-grid-based-pathfinding-a.html). stairs as graph edges
