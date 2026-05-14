---
title: Timberbot Guide
description: Full operating guide for Claude and other LLMs playing Timberborn via timberbot.py.
version: "0.8.5"
---
# Timberbot Guide

This is the full Timberbot Guide for playing Timberborn through `timberbot.py`.

`agents/timberbot.md` is the Timberbot agent: the slim runtime prompt injected at launch. This page is the full guide behind that prompt. The split keeps launch tokens low while preserving the deeper operating rules and reference material the agent may need.

Read this first. Use the other docs only when needed:

- [API Reference](api-reference.md) for exact commands, endpoint shapes, helper behavior, pagination, and error payloads
- [Getting Started](getting-started.md) for install, PATH, remote host, and troubleshooting

## FIRST RUN: Boot Sequence

On the first invocation of `/timberbot` per session, complete `Boot`, then `Brain`, in order. The boot report is not a game action. It proves that you loaded the guide before making changes.

### Boot (rules confirmation. NO API calls)

`timberbot.py` is on PATH. Run it directly. never use `python` prefix, never `cd` anywhere.

1. Read this entire guide top to bottom before calling any game APIs.
2. Read `api-reference.md` top to bottom. Once in context, use it from context. do not re-read it.
3. Immediately print this boot report in markdown. Replace every placeholder yourself. Use lowercase throughout:

```md
## TIMBERBOT

> **docs source** `<full docs directory path | github docs url with user approval>`
> **ai doc** `<full path | MISSING>`
> **api doc** `<full path | MISSING>`
> **setup doc** `<full path | MISSING>`

- **boot rule** `<when boot runs>`
- **first read** `<first command to read colony state>`
- **placement** `<required building placement method>`
- **crops** `<required crop planning method>`
- **mutations** `<mutation execution rule>`
- **path root** `<what path distance is measured from>`
- **prefabs** `<prefab naming rule. include the lookup requirement>`
- **power** `<power routing rule>`
- **errors** `<error format rule>`
- **remote** `<remote connection rule>`

> **boot result** `<PASSED | FAILED>`
```

If boot is `PASSED`, continue immediately to `Brain`.
If any doc is `MISSING`, any placeholder is left blank, or any fact cannot be stated, boot is `FAILED`. Report the issue, ask the user for guidance, and do not make any game API calls.
### Brain (pre-loaded. do NOT call brain again)

Colony state was already gathered and injected into your system prompt before this session started. Look for the `## CURRENT COLONY STATE` section in your context. Do NOT run `timberbot.py brain`. it was already run for you.

3. Print this readout using the colony state from your system prompt:

```md
> **settlement** `<name>` | `<faction>` | <"new" or "loaded">
> **goal** `<goal text or "none. awaiting orders">`
>
> **day** `<N>` | **speed** `<S>` | **weather** <temperate/drought> `<N>d` remain
> **pop** `<adults>` adults, `<children>` children | **beds** `<occ>`/`<total>` | **workers** `<assigned>`/`<vacancies>`
> **supply** food `<F>d` | water `<W>d` | logs `<L>d` | planks `<P>d` | gears `<G>d`
> **wellbeing** `<avg>`/77 | `<miserable>` miserable | `<critical>` critical (beavers only)
> **nearby** `<N>` trees (`<species>`) | `<N>` food (`<species>`)
> **alerts** `<unstaffed>` unstaffed | `<unpowered>` unpowered | `<unreachable>` unreachable
>
> **tasks** <count pending/active/failed or "none">
```

If food or water is `<= 1d`, append `CRITICAL` after the value. If alerts are all zero, show `all clear`.

**Note on stats:** Wellbeing averages, "miserable" counts, "critical" counts, and survival days (food/water/logs/planks/gears) strictly reflect **organic beavers** only. Bots are excluded to ensure statistics represent actual colony survival and health.

If the colony state section is missing from your system prompt, fall back to running `timberbot.py brain goal:"<goal>"` manually.

4. If there are failed or active tasks from a previous session, list them and assess whether to retry or re-plan before starting new work.

Only after both phases are complete should you begin working on the user's request.

On subsequent invocations in the same session, skip the boot sequence and go straight to work.

---

This is a human-AI co-op game. The human player is also building, demolishing, and changing settings in real time. Game state can change between API calls.

`timberbot.py` is on PATH. Call it directly. See [Getting Started](getting-started.md) for setup details.

Beavers die if food or water hits 0.

## HARD RULE: Sequential execution

Never run mutating game API calls in parallel. Every placement, path, or config call changes the map state that the next call depends on. A failed placement invalidates every subsequent call that assumed it succeeded.

Read-only calls like `brain`, `buildings`, `find_placement`, `map`, `tiles`, and `weather` can run in parallel if you truly need that, but any mutating call such as `place_building`, `place_path`, `demolish_building`, `demolish_crop`, `set_*`, `plant_crop`, or `mark_trees` must complete and succeed before the next action.

## Roads

Roads are the circulatory system of the colony. They cost nothing and can be placed freely. Every building, workplace, and resource site must connect back to the district center through an unbroken chain of path tiles. Without roads, beavers are trapped at the DC and cannot reach work, haul, or access water.

Think of the road network as a tree:

- the DC entrance is the root
- trunk roads extend outward from it
- branch roads fork toward resources and building sites
- each building attaches via its entrance tile

The DC entrance is on the side matching the DC orientation. For a south-facing DC at `(x, y)`, the entrance is the middle tile of the south edge: `(x+1, y-1)`.

`find_placement.reachable` means "path-connected to DC." `find_placement.distance` is the path cost from the DC entrance through the flow field. A building with `reachable:0` cannot be staffed or supplied. Lower distance means shorter hauler trips.

## Placement

Use `find_placement` for all building placement. Never manually search tiles, grep for water, or scan the map by eye for final coordinates. It checks terrain, water depth, flooding, orientation, path adjacency, and reachability in one call. Use the `x`, `y`, `z`, and `orientation` it returns.

**Proximity filtering:** When searching for buildings, trees, or placements, the `radius` filter (default: 30) **requires both x and y** coordinates to be provided. Providing only one coordinate will disable the proximity filter to avoid accidental `y=0` defaults.

`brain` returns the DC coords, entrance, z-level, and a 41x41 ANSI map centered on the DC. The saved map file (`memory/map-districtcenter-*.txt`) shows terrain, water, trees, and buildings.

Paths are 1x1 entities that occupy tiles. A path on a tile prevents any building from being placed there.

The tile one step in the orientation direction from the entrance must be a path. `find_placement` returns `orientation` pointing the entrance toward the nearest path.

Entrance directions:

- north = `+y`
- south = `-y`
- east = `+x`
- west = `-x`

Example: building at `(10,10)` with orientation `south` means the entrance faces `-y`, so tile `(10,9)` must be a path.

Important planning fields:

- `entranceX` / `entranceY`: tile where a path must go
- `flooded`: whether the footprint floods
- `waterDepth`: useful for pumps and water structures
- `reachable`: whether the site connects back to the DC
- `pathAccess`: whether the site already has a path-adjacent entrance
- `nearPower`: whether the site already touches the power network
- `distance`: path cost from the DC entrance via flow field

Sort preference: non-flooded > reachable > lower distance > pathAccess > nearPower.

`z` must equal the terrain height at the placement location. Wrong `z` causes invisible or broken placement. Use the brain map for fast orientation and `tiles` when you need raw data.

A new game starts with only a district center and no roads. Paths cost nothing. Stairs and platforms require science unlocks.

## Game Speed

| Level | Name | Effect |
|-------|------|--------|
| 0 | **Paused** | No time passes. Placement, pathing, and priority changes still work. |
| 1 | **Normal** | 1x speed |
| 2 | **Fast** | 2x speed |
| 3 | **Fastest** | 4x speed |

Use speed 0 to plan and queue work without spending resources. Do not unpause without a reason.

## Automation (1.0+)

Timberborn 1.0 introduced a full automation system: sensors detect conditions, relays process logic, and wires connect them to buildings. The API exposes wiring and configuration for AI agents.

> **Vocabulary warning.** Timberborn reuses "high" and "low" across three unrelated systems (automation signals, priorities, physical heights). Mixing them produces hallucinated commands. Read [`automation-states.md`](automation-states.md) for the disambiguation. The short version: automation wires carry only `On`/`Off`, priorities use `VeryLow`..`VeryHigh`, and floodgate heights are floats.

### Automation buildings

| Building | What it does |
|---|---|
| Depth Sensor | Triggers when water reaches a threshold |
| Contamination Sensor | Triggers when badwater contamination reaches a threshold |
| Flow Sensor | Triggers when water flow reaches a threshold |
| Resource Counter | Triggers when a good's stock or fill rate meets a threshold |
| Population Counter | Triggers when beaver/bot population meets a threshold |
| Power Meter | Triggers when power production or consumption meets a threshold |
| Relay | Combines two inputs with logic (Not/And/Or/Xor/Passthrough) |
| Memory | Stores input state; reset via a third input |
| Timer | Fires on a schedule |
| Chronometer | Fires between start and end times |
| Lever | Binary on/off output; can be spring-return or pinned |
| Gate | Pauses or unpauses flow through a waterway |

### How wiring works

1. A **sensor/relay** has an `Automator` component (it is a transmitter).
2. A **building** (pump, factory, gate, etc.) has an `Automatable` component (it accepts a wire).
3. The API `link` call connects the transmitter's output to the building's pause input.
4. For **relays and memory**, there are two inputs (A and B) and optionally a reset.

### Configuring sensors

Each sensor has a threshold, a comparison mode (Equal, Greater, Less, etc.), and type-specific options:

- **Resource Counter**: also needs `goodId` (e.g., `"Water"`, `"Logs"`, `"Carrot"`) and optionally `fillRateThreshold`
- **Population Counter**: can count beavers only, bots only, or both; can be global or district-local
- **Power Meter**: can use absolute (int) or percentage thresholds

### API commands

See `docs/api-reference.md` for the full endpoint reference. The key commands are:

- `timberbot.py link source_id:<id> target_id:<id> [input:a|b]` — wire a sensor/relay to a building or relay input
- `timberbot.py unlink target_id:<id> [input:a|b|reset]` — remove a wire
- `timberbot.py configure_automation id:<id> property:<prop> value:<val>` — set threshold, mode, goodId, etc.

### Reading automation state

Buildings with automation have an `automation` block in their JSON response:

```json
{
  "id": 42,
  "name": "ContaminationSensor",
  "automation": {
    "type": "ContaminationSensor",
    "state": "On",
    "config": { "threshold": 0.5, "comparisonMode": "Greater" },
    "outputs": [{"id": 44, "name": "Relay #1"}]
  }
}
```

A regular building with a wire looks like:

```json
{
  "id": 44,
  "name": "WaterPump",
  "automation": {
    "isAutomated": true,
    "inputState": "On",
    "input": {"id": 42, "name": "ContaminationSensor"}
  }
}
```

For full API details, read `docs/api-reference.md` and `docs/automation-plan.md` (which contains the decompiled game API surface).

## Brain

`brain` is the preferred first read for colony state because it combines live game state with persistent task and memory state.

- `timberbot.py brain` returns live summary from the game plus goal/tasks/locations from disk
- `timberbot.py brain goal:"<text>"` sets or overwrites the persistent goal

### What brain returns

**Live summary (fresh every call):**

- settlement name and faction
- day, progress, speed, and weather
- district population/resources/housing/employment/wellbeing/DC
- trees and crops, including species breakdowns
- wellbeing summary and per-category values
- science, alerts, building role counts
- nearby `treeClusters` and `foodClusters`

**Persistent state (stored in `memory/<settlement>/brain.toon`):**

- `goal`
- ordered `tasks`
- saved `locations`
- last write timestamp

### Districts and clusters

Each district is self-contained: population, resources, housing, employment, wellbeing, and DC coords. Multi-district colonies return all districts.

`treeClusters` and `foodClusters` are filtered to the same z-level as the first DC and within 40 Manhattan distance. `grown` means harvestable now. `total` includes seedlings or immature growth.

### Locations

`brain` auto-seeds named locations (DC, forests, berries) from live data on first run. Use `set_location` and `remove_location` to manage them. Locations are stored in `brain.toon` and survive between sessions.

### Task workflow

- Use persistent tasks for multi-step work that will span turns or sessions.
- Mark failed tasks with the real reason so the next session can re-plan instead of repeating the same mistake.

## Factions and prefab names

Timberborn has two factions: **Folktails** and **Iron Teeth**. Every prefab except `Path` and the reserve storage buildings requires a faction suffix (`.Folktails` or `.IronTeeth`). Never use a bare name like `GathererFlag`; it must be `GathererFlag.Folktails` or `GathererFlag.IronTeeth`.

Use `brain` or summary output to confirm the current faction before planning buildings.

ALWAYS run `timberbot.py prefabs | grep -i <keyword>` before placing any building you have not already placed this session. NEVER guess a prefab name by swapping faction suffixes. Names are inconsistent across factions:

- Folktails `SmallPile` -> Iron Teeth `SmallIndustrialPile` (not SmallPile)
- Folktails `LumberMill` -> Iron Teeth `IndustrialLumberMill` (not LumberMill)
- Folktails `EfficientFarmHouse` -> Iron Teeth `FarmHouse` (reversed)
- Folktails `SmallWarehouse` -> Iron Teeth `MediumWarehouse` (different size prefix)

A wrong prefab name causes `invalid_prefab` on every placement attempt. If `find_placement` returns `invalid_prefab`, check the prefab name FIRST.

### Folktails key buildings

| Role | Prefab name | Notes |
|---|---|---|
| Housing | Lodge.Folktails | 2x2 starter housing |
| Housing | MiniLodge.Folktails | 1x1, science unlock |
| Housing | DoubleLodge.Folktails | science unlock |
| Housing | TripleLodge.Folktails | science unlock |
| Farming | EfficientFarmHouse.Folktails | land crops; there is no `FarmHouse.Folktails` |
| Farming | AquaticFarmhouse.Folktails | aquatic crops |
| Water | WaterPump.Folktails | must straddle land/water edge |
| Water | LargeWaterPump.Folktails | science unlock |
| Power | WaterWheel.Folktails | needs flowing water |
| Power | WindTurbine.Folktails | science unlock |
| Power | LargeWindTurbine.Folktails | science unlock |
| Wood | LumberjackFlag.Folktails | chops trees |
| Wood | LumberMill.Folktails | logs to planks |
| Wood | Forester.Folktails | plants trees, science unlock |
| Food processing | Grill.Folktails | grilled foods |
| Food processing | Gristmill.Folktails | flour |
| Food processing | Bakery.Folktails | bread |
| Storage | SmallWarehouse.Folktails | starter warehouse |
| Storage | SmallTank.Folktails | starter water storage |
| Storage | SmallPile.Folktails | starter log pile |
| Science | Inventor.Folktails | science generation |
| Leisure | Campfire.Folktails | SocialLife +1 |
| Infrastructure | DistrictCenter.Folktails | colony hub |
| Infrastructure | HaulingPost.Folktails | hauler workplace |
| Infrastructure | Dam.Folktails | starter water retention |
| Infrastructure | Levee.Folktails | full water block |
| Infrastructure | Stairs.Folktails | z-level transition |
| Infrastructure | Platform.Folktails | multi-level jump |
| Infrastructure | PowerShaft.Folktails | power transmission |
| Infrastructure | GathererFlag.Folktails | gathers berries |
| Infrastructure | ScavengerFlag.Folktails | collects scrap metal |
| Wellbeing | RooftopTerrace.Folktails | SocialLife +1 |
| Utility | TeethGrindstone.Folktails | fixes chipped teeth |
| Utility | MedicalBed.Folktails | heals injuries |

### Iron Teeth key buildings

| Role | Prefab name | Notes |
|---|---|---|
| Housing | Rowhouse.IronTeeth | starter housing |
| Housing | Barrack.IronTeeth | science unlock |
| Farming | FarmHouse.IronTeeth | land crops |
| Farming | HydroponicGarden.IronTeeth | indoor, needs power and science |
| Water | DeepWaterPump.IronTeeth | 3x2, must straddle land/water edge |
| Power | LargePowerWheel.IronTeeth | 300hp hamster wheel |
| Power | SteamEngine.IronTeeth | science unlock |
| Wood | LumberjackFlag.IronTeeth | chops trees |
| Wood | IndustrialLumberMill.IronTeeth | logs to planks (NOT LumberMill) |
| Wood | Forester.IronTeeth | plants trees |
| Storage | SmallIndustrialPile.IronTeeth | starter log pile (NOT SmallPile) |
| Storage | SmallTank.IronTeeth | starter water storage |
| Storage | MediumWarehouse.IronTeeth | starter warehouse (NOT SmallWarehouse) |
| Science | Inventor.IronTeeth | science generation |
| Leisure | Campfire.IronTeeth | SocialLife +1 |
| Wellbeing | Scratcher.IronTeeth | Fun +1 |
| Infrastructure | HaulingPost.IronTeeth | hauler workplace |
| Infrastructure | GathererFlag.IronTeeth | gathers berries |
| Infrastructure | ScavengerFlag.IronTeeth | collects scrap metal |

### Shared buildings

`Path`, `AncientAquiferDrill`, `ReservePile`, `ReserveTank`, and `ReserveWarehouse` do not use faction suffixes.

## Path routing and placement hazards

### Path routing

`place_path` uses A* pathfinding over a 3D surface graph. It routes around buildings, natural resources, ruins, water, and terrain obstacles. It handles diagonal routes, multi-z transitions with auto-stairs, and reuses existing paths/stairs/platforms.

Parameters:

- `x1, y1, x2, y2`: start and end coordinates
- `style`: `"direct"` (default, shortest path) or `"straight"` (prefers straight lines when costs are equal)
- `sections`: stop after N stair/ramp crossings (0 = unlimited). Useful for building paths incrementally across elevation changes.

Paths cost nothing; use them freely. Stairs and platforms require science unlocks. Without those unlocks, vertical routing stops at z-changes.

The path planner treats ruins and map editor objects as impassable, the same as buildings. If a path seems blocked by invisible obstacles, check `tiles` for ruin occupants.

### Flooding

Buildings placed in water become flooded and completely non-functional. Beavers and bots cannot access them.

- Most buildings flood when any water touches their footprint tile.
- Paths, power shafts, landscaping, stream gauges, Zipline/Tubeway Stations, Gravity Battery, Numbercruncher, and Control Tower are immune.
- Flooded tiles with badwater contamination also poison beavers who walk through them.
- Flooded buildings resume when water recedes; there is no permanent damage.
- `find_placement.flooded` is the signal to trust. Terrain height alone is not reliable.

### Priorities and footprints

Buildings have separate construction and workplace priority concepts. Reserve high priority for true bottlenecks rather than marking everything urgent.

Common footprints:

- Common: DistrictCenter 3x3, HaulingPost 3x2, Inventor 2x2, Forester 2x2, flags 1x1, Path 1x1
- Folktails: Lodge 2x2, EfficientFarmHouse 2x2, WaterPump 2x3, LumberMill 2x3, Shower 1x2
- Iron Teeth: Rowhouse 1x2, Barrack 3x2, FarmHouse 2x2, DeepWaterPump 3x2, LargePowerWheel 3x3, IndustrialLumberMill 2x3, DoubleShower 1x2

## Game mechanics

### Time pressure

- The game clock runs continuously. Every in-game hour spent idle is an hour beavers are not eating, drinking, or building.
- Drought arrives on a fixed schedule. Unspent time before a drought is time that could have been used to stockpile water and food.
- There is almost always something productive to do: mark trees, place a building, assign workers, adjust priorities, plant crops, expand roads.
- Inaction compounds. A 2-day gap with no actions can mean 2 fewer days of crop growth, 2 fewer days of resource hauling, and 2 fewer days of construction progress.
- When in doubt, check `alerts` for unstaffed/unpowered/unreachable buildings, or `resources` for low stocks.
- Pausing (speed 0) is available for planning, but the game only progresses when unpaused. A paused game produces no food, no water, no construction progress.
- Unpause AFTER giving beavers work, not before. If all workers are unemployed, unpausing just wastes food/water while beavers idle. Place buildings, assign workers, build roads FIRST at speed 0, then unpause.
- The summary shows `unemployed` count. If unemployed > 0 and vacancies > 0, beavers need roads to reach workplaces or workers need to be assigned.
- Speed 0 for extended periods means nothing happens. Use speed 1-3 to keep the colony alive once beavers have work.

### Food

- Beavers eat about 1 food per day.
- Wild berries are finite; gatherers deplete them.
- Rough rule: 1 farmhouse per 8 beavers with full fields.
- Crops need irrigated soil. Crops continue growing during drought if the soil stays moist.
- Use `find_planting`; do not guess irrigated zones.

**Folktails crops:**

| Crop | Growth | Processing | Nutrition |
|---|---|---|---|
| Carrot | 4 days (2.8 w/ beehive) | raw | +1 |
| Sunflower | 5 days (3.5 w/ beehive) | raw (seeds) | +1 |
| Potato | 6 days (4.2 w/ beehive) | Grill -> GrilledPotatoes | +2 |
| Wheat | 10 days (7 w/ beehive) | Gristmill -> flour -> Bakery -> Bread | +2 |
| Cattail | 8 days (5.6 w/ beehive) | aquatic -> CattailCracker | +2 |
| Spadderdock | 12 days (8.4 w/ beehive) | aquatic, Grill -> GrilledSpadderdock | +2 |
| Chestnut | tree, 24 days | Grill -> GrilledChestnuts | +2 |
| Maple | tree, 28 days | tree tap -> MaplePastry | +3 |

**Iron Teeth crops:**

| Crop | Growth | Processing | Nutrition |
|---|---|---|---|
| Kohlrabi | 3 days | raw | +1 |
| Mangrove Fruit | tree, 10 days | raw | +1 |
| Cassava | 5 days | Fermenter -> FermentedCassava | +2 |
| Soybean | 8 days | Fermenter -> FermentedSoybean | +2 |
| Corn | 10 days | FoodFactory -> CornRation | +2 |
| Eggplant | 12 days | FoodFactory -> EggplantRation | +2 |
| Canola | 9 days | -> oil (processing) | +2 |
| Coffee | bush (Forester) | roasted, doesn't satisfy hunger | +3 |

Berries are a bridge to farming, not a long-term food plan.

**Beehive** (Folktails only) boosts crop growth about 30% in a 3-tile radius. It does not boost trees or bushes.

### Water

- Water is the top priority. Beavers die without water faster than without food. Place water pumps and route paths to them BEFORE placing other buildings. Other buildings placed near the water's edge consume waterfront tiles that pumps need.
- Pumps must straddle the land/water edge. Waterfront tiles are limited and irreplaceable. once a path or non-pump building occupies a waterfront tile, no pump can go there. Reserve waterfront tiles for pumps.
- Place paths and non-water buildings away from the water's edge. Route paths around waterfront, not along it.
- Folktails use `WaterPump.Folktails` or `LargeWaterPump.Folktails`.
- Iron Teeth use `DeepWaterPump.IronTeeth`.
- Run `find_placement` with the water pump prefab to discover all valid pump locations. Results with `waterDepth > 0` are waterfront tiles with water access. Use these positions for pumps. they are the only tiles where pumps can function.
- If valid pump positions are on the same z-level as the DC, prioritize those. shorter haul distance for water delivery.
- Rough rule: 2 pumps per 15 beavers, 3 pumps once you are above that and preparing for drought.
- During drought, water is consumed but not produced; only stored water and aquifer drills help.

### Irrigation and moisture

- Water tiles irrigate nearby ground.
- A 1x1 pond irrigates only a short radius; larger connected bodies irrigate farther.
- Elevation changes reduce irrigation range significantly.
- If soil dries out, plants wither and die.
- `tiles` output includes `moist: true`.

### Water management structures

- **Dam**: cheap early retention, spills above 0.65 height
- **Levee**: watertight wall, stackable, beavers can walk on it
- **Floodgate**: adjustable water control, no walking on top

### Aquifer drills

- **Ancient Aquifer Drills** are map-placed and need 400hp.
- **Aquifer Drill** is the player-built version and also needs 400hp.
- Aquifer drills keep producing during drought.

### Badwater and contamination

- Badwater contaminates beavers and kills plants.
- It is also a mid-game resource for extract and explosives chains.
- Folktails clean contamination with Herbalists; Iron Teeth use Decontamination Pods.

### Trees

| Tree | Growth | Logs | Special | Faction |
|---|---|---|---|---|
| Birch | 7 days | 1 | - | both |
| Pine | 12 days | 2 | Pine Resin | both |
| Oak | 30 days | 8 | - | both |
| Chestnut | 24 days | 4 | Chestnuts (food) | Folktails |
| Maple | 28 days | 6 | Maple Syrup (food) | Folktails |
| Mangrove | 10 days | 2 | Mangrove Fruits (food) | Iron Teeth |

Birch is best early. Oak is strongest long-term.

`tree_clusters` finds the densest grown tree clusters. `food_clusters` finds the densest gatherable food clusters (berries, bushes), excluding trees. `markedGrown` in `brain` is the immediately choppable supply.

**Forester** plants trees and bushes on moist soil. One forester keeps up with about four lumberjacks.

### Power

- Power transfers through adjacent buildings only.
- Paths do not conduct power.
- Oasis maps have standing water, so manual power is often better than water wheels.
- `find_placement.nearPower` helps with adjacency checks.

### Manufacturing chains

Construction supply chain:

```text
Trees -> Logs -> Planks -> most buildings
Planks -> Gears -> advanced buildings and bot parts
ScrapMetal -> MetalBlocks -> late-game buildings
```

Planks are usually the first bottleneck. Gears are the second.

Folktails knowledge chain: Paper -> Books

Extract chain: Badwater + Logs -> Extract

Biofuel chain (Folktails): Crops + Water -> Biofuel. Timberbots consume about 2 biofuel per day.

### Beaver lifecycle

- Lifespan is about 50 days.
- Kits mature in 6 days base, faster with high wellbeing.
- Beavers sleep at home during non-work hours.
- `TeethGrindstone` fixes chipped teeth.

### Weather cycles

- Every cycle has one temperate phase and one hazardous phase.
- Hazardous weather is drought or badtide.
- Durations escalate over time.
- Games always start in temperate weather.

### Population growth

- Folktails reproduce naturally through housing.
- Iron Teeth use Breeding Pods.

### Bots

- Bots work 24/7 and ignore food, water, sleep, and wellbeing.
- Folktails bots need Biofuel.
- Iron Teeth bots need Charging Stations.
- Bots cannot work at Power Wheels or Inventors.

### Scaling ratios (approximate)

| Per-capita | Ratio | Notes |
|---|---|---|
| Farmhouse | 1 per 8 beavers | with full irrigated fields |
| Water pump | 1 per 7 beavers | more during drought prep |
| Housing | depends on building | Lodge (FT) holds ~4, Rowhouse (IT) holds 2 |
| Lumberjack | 1 per 15 beavers | keep staffed always |
| Water tanks | 30 water per drought-day per beaver | plan for longest expected drought |

### Storage

Three storage families exist:

- Piles for logs, planks, metal blocks, and dirt
- Warehouses for food, gears, and manufactured goods
- Tanks for water, badwater, extract, syrup, and biofuel

Each storage holds one good type at a time. Configure filters and capacities with the API when needed.

### Districts

Districts are separate colonies connected by District Crossings. Resources do not automatically flow between them.

- Expand when work clusters are too far from the DC.
- Use distribution controls for goods flow between districts.
- Use migration controls for population transfers.
- Every district needs its own basic survival chain.

### Death spiral

When food or water hits zero: beavers die -> fewer workers -> less production -> more die.

### Workers

- Hauling requires idle or unemployed beavers.
- Too many staffed buildings with too few workers means nobody hauls.
- Lumberjacks must stay staffed.
- Default work hours end at 18.

## Wellbeing

Wellbeing categories and caps differ by faction. Use the API when you need the exact live breakdown, but use the tables below for planning and prioritization.

### Folktails wellbeing (max 77)

| Category | Max | Needs (building/food -> bonus) |
|---|---|---|
| BasicNeeds | 5 | Hunger (+1), Thirst (+1), Sleep (+1), Shelter (+1), WetFur (+1 via Shower.Folktails) |
| SocialLife | 11 | Campfire (+1), ContemplationSpot (+1), RooftopTerrace (+1), Agora (+3), DanceHall (+5) |
| Fun | 8 | Detailer (+1), Lido (+1), Carousel (+3), MudPit (+3) |
| Nutrition | 15 | Carrots (+1), SunflowerSeeds (+1), GrilledPotatoes (+2), GrilledChestnuts (+2), GrilledSpadderdock (+2), Bread (+2), CattailCracker (+2), MaplePastry (+3) |
| Aesthetics | 9 | Shrub (+1), Lantern (+1), Roof (+1), Scarecrow (+1), Weathervane (+1), BeaverStatue (+2), BulletinPole (+2) |
| Knowledge | 3 | Books (+3 via PrintingPress) |
| Awe | 26 | FarmerMonument (+3), BrazierOfBonding (+5), FountainOfJoy (+8), EarthRecultivator (+10) |

### Iron Teeth wellbeing (max ~77)

| Category | Max | Needs (building/food -> bonus) |
|---|---|---|
| BasicNeeds | 5 | Hunger (+1), Thirst (+1), Sleep (+1), Shelter (+1), WetFur (+1 via DoubleShower.IronTeeth) |
| SocialLife | 2 | Campfire (+1), RooftopTerrace (+1) |
| Fun | 17 | Scratcher (+1), SwimmingPool (+1), ExercisePlaza (+3), MudBath (+3), WindTunnel (+3), Motivatorium (+5) |
| Nutrition | 17 | Kohlrabi (+1), MangroveFruits (+1), FermentedCassava (+2), FermentedSoybean (+2), FermentedMushroom (+2), CornRation (+2), EggplantRation (+2), AlgaeRation (+2), Coffee (+3, no hunger) |
| Aesthetics | 10 | Lantern (+1), Brazier (+1), Shrub (+1), Roof (+1), BeaverBust (+1), BeaverStatue (+2), Bell (+1), DecorativeClock (+2) |
| Awe | 26 | LaborerMonument (+3), FlameOfUnity (+5), TributeToIngenuity (+8), EarthRepopulator (+10) |

Nutrition requires food variety. Different food types matter; more of one food does not.

### Wellbeing building placement

- Each wellbeing building has an `effectRadius`.
- Place wellbeing buildings near high-traffic areas.
- Different wellbeing types can overlap productively.
- Duplicating the same wellbeing building in the same area is usually wasted coverage.

### Recipe switching

Changing recipes destroys in-progress items and consumed materials. Re-setting the same recipe also resets progress. Treat recipe changes as expensive and avoid churning them.

### Rooftop buildings

- Decorative roofs provide aesthetics just by existing and do not need path access.
- `RooftopTerrace` is an actual visited building and does need path or stair access.

## Conditional References

Use these docs when you need more than the AI guide itself:

- [API Reference](api-reference.md) for exact commands, query parameters, pagination, helper behavior, and error payloads
- [Getting Started](getting-started.md) for install, PATH, remote host, Steam Workshop paths, and troubleshooting guidance
