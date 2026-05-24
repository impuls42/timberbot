# Thread-Safe Surfaces

> **Status:** implementation notes — current.

Concrete Timberborn thread-safety guidance for Timberbot development.

This document is based on live inspection through Timberbot's `/api/debug` endpoint on a running game, plus the current Timberbot architecture. It distinguishes between:

- explicitly thread-safe game services,
- services that appear snapshot-backed and may be usable off-thread but should be verified per method,
- normal gameplay/entity services that should be treated as main-thread only.

## Safe off-thread

These are the surfaces Timberborn exposes as explicitly thread-safe, or Timberbot publishes as read-only snapshots.

### Timberborn thread-safe services

#### `IThreadSafeWaterMap`

Runtime type observed via `/api/debug`:

`Timberborn.WaterSystem.ThreadSafeWaterMap`

Confirmed read methods:

- `ColumnCount(int)`
- `ColumnFloor(int)`
- `ColumnCeiling(int)`
- `WaterDepth(int)`
- `WaterDepth(Vector3Int)`
- `ColumnContamination(Vector3Int)`
- `WaterFlowDirection(Vector3Int)`
- `CeiledWaterHeight(Vector3Int)`
- `WaterHeightOrFloor(Vector3Int)`
- `CellIsUnderwater(Vector3Int)`
- `TryGetColumnFloor(Vector3Int, out int)`

Confirmed read properties:

- `ColumnCounts`
- `WaterColumns`
- `FlowDirections`

#### `IThreadSafeColumnTerrainMap`

Runtime type observed via `/api/debug`:

`Timberborn.TerrainSystem.ThreadSafeColumnTerrainMap`

Confirmed read methods:

- `GetColumnCount(int)`
- `GetColumnCeiling(int)`
- `GetColumnFloor(int)`
- `TryGetIndexAtCeiling(int, int, out int)`
- `TryGetIndexAtOrAboveCeiling(int, int, out int)`

Confirmed read properties:

- `ColumnCounts`
- `TerrainColumns`

### Timberbot snapshot data

These are not live Timberborn objects. They are Timberbot-owned published snapshot surfaces and are safe to read off-thread within Timberbot's current design.

- `ReadV2.Buildings`
- `ReadV2.Beavers`
- `ReadV2.NaturalResources`
- `ReadV2.Districts`
- published primitive/string fields inside the snapshot DTOs
- published immutable/shareable data such as building tile footprints

## Verify first

These services are not explicitly named thread-safe, but live inspection shows they are backed by thread-safe terrain references and internal arrays. They may be safe for specific read methods, but do not treat them as universally thread-safe without proving the specific call path.

### `SoilMoistureService`

Observed internal fields:

- `_threadSafeColumnTerrainMap:IThreadSafeColumnTerrainMap`
- `_threadSafeMoistureLevels:Single[]`

Observed read methods:

- `SoilMoisture(int)`
- `SoilIsMoist(Vector3Int)`

### `SoilContaminationService`

Observed internal fields:

- `_threadSafeColumnTerrainMap:IThreadSafeColumnTerrainMap`
- `_threadSafeContaminationLevels:Single[]`

Observed read methods:

- `Contamination(int)`
- `SoilIsContaminated(Vector3Int)`

### Guidance for this bucket

- Use `/api/debug` to inspect backing fields before relying on a method off-thread.
- Prefer methods that read thread-safe arrays or maps directly.
- Avoid methods that traverse entity/component graphs, registries, or mutable gameplay collections.
- If a method is important to a public endpoint, add a debug probe or validation path before treating it as safe.

## Main-thread only

Treat these as main-thread only unless there is specific contrary evidence for a particular method.

### Normal gameplay and registry services

- `BuildingService`
- `DistrictCenterRegistry`
- `EntityRegistry`
- `FactionNeedService` (but `GetBeaverNeeds()`/`GetBotNeeds()` are read-only LINQ filters over a write-once list. effectively safe for reads after `Load()`. Timberbot caches the result at load time)
- `ScienceService`
- `BuildingUnlockingService`
- `NotificationSaver`
- `INavMeshService`
- `PlantingService`
- `TreeCuttingArea`
- `PreviewFactory`
- `BlockObjectPlacerService`
- `EntityService`

### Live entity/component data

- `EntityComponent`
- `GetComponent<T>()` results
- building, beaver, and natural resource component properties
- live district/building/beaver registries and lists
- any normal `List<T>` or `ReadOnlyList<T>` exposed by gameplay services

## Building-specific finding

There is no generic thread-safe building surface currently known in Timberborn.

Live runtime inspection and managed-assembly reflection found:

- `BuildingService`
  - `_buildings: List<>`
  - `Buildings: ReadOnlyList<>`
- `DistrictBuildingRegistry`
  - `_finishedBuildings: EntityComponentRegistry`
  - `_instantFinishedBuildings: EntityComponentRegistry`
  - `GetEnabledBuildings() -> IEnumerable<>`
  - `GetEnabledBuildingsInstant() -> IEnumerable<>`

These are live registries or read-only wrappers over mutable collections. They are not thread-safe building snapshots.

### Important exception: water sources

Timberborn does have one narrow building-adjacent snapshot pattern:

- `WaterSourceRegistry`
  - `_waterSources: List<>`
  - `_threadSafeWaterSources: List<>`
  - `ThreadSafeWaterSources: ReadOnlyList<>`
  - `UpdateThreadSafeRegistry()`
- `ThreadSafeWaterSource`
  - specialized snapshot wrapper over `IWaterSource`

This is the clearest example of how Timberborn handles off-thread access for building-like systems:

- not a universal thread-safe building API,
- but a specialized thread-safe projection for the exact subsystem that needs it.

### Implication

If Timberbot wants leaner off-thread building reads, the closest game-native pattern is:

- keep using explicit thread-safe map services directly,
- keep using Timberbot snapshots for general building/beaver/entity data,
- consider specialized building projections for narrow cases instead of assuming a hidden generic building snapshot API exists.

### Suggested Timberbot building projection split

The current `CachedBuilding` mixes three different concerns:

- static building identity/geometry,
- periodic mutable building state,
- heavy nested detail payloads used only by full-detail endpoints.

The leaner split is:

#### `BuildingDefinition`

Static or event-driven data:

- `Id`, `Name`
- `X`, `Y`, `Z`
- `Orientation`
- `HasFloodgate`, `FloodgateMaxHeight`
- `HasClutch`, `HasWonder`
- `IsGenerator`, `IsConsumer`
- `NominalPowerInput`, `NominalPowerOutput`
- `EffectRadius`
- `OccupiedTiles`
- `HasEntrance`, `EntranceX`, `EntranceY`

#### `BuildingState`

Periodic snapshot data:

- `Finished`, `Paused`, `Unreachable`, `Powered`
- `District`
- `AssignedWorkers`, `DesiredWorkers`, `MaxWorkers`
- `Dwellers`, `MaxDwellers`
- `FloodgateHeight`
- `ConstructionPriority`, `WorkplacePriorityStr`
- `BuildProgress`, `MaterialProgress`, `HasMaterials`
- `ClutchEngaged`, `WonderActive`
- `PowerDemand`, `PowerSupply`, `PowerNetworkId`
- `CurrentRecipe`, `ProductionProgress`, `ReadyToProduce`
- `NeedsNutrients`
- `Stock`, `Capacity`

#### `BuildingDetailState`

Nested payloads used only by full-detail reads:

- `Inventory`
- `Recipes`
- `NutrientStock`

#### Fields that should stay out of the published read model

These are main-thread cache maintenance fields, not off-thread API data:

- `Entity`
- `BlockObject`
- `Pausable`
- `Floodgate`
- `BuilderPrio`
- `Workplace`
- `WorkplacePriority`
- `Reachability`
- `Mechanical`
- `Status`
- `PowerNode`
- `Site`
- `Inventories`
- `Wonder`
- `Dwelling`
- `Clutch`
- `Manufactory`
- `BreedingPod`
- `RangedEffect`
- `DistrictBuilding`
- `LastDistrictRef`

This projection split is closer to Timberborn's own apparent pattern:

- static/template-like data stays stable,
- narrow runtime state is snapshotted,
- special subsystems get specialized thread-safe projections when needed.

### Suggested Timberbot beaver projection split

`CachedBeaver` is an even cleaner candidate for a projection split than `CachedBuilding`.

#### `BeaverDefinition`

Stable identity data:

- `Id`
- `Name`
- `IsBot`

#### `BeaverState`

Periodic snapshot data:

- `Wellbeing`
- `X`, `Y`, `Z`
- `Workplace`
- `District`
- `HasHome`
- `Contaminated`
- `LifeProgress`
- `DeteriorationProgress`
- `IsCarrying`
- `CarryingGood`
- `CarryAmount`
- `LiftingCapacity`
- `Overburdened`
- `AnyCritical`

#### `BeaverDetailState`

Nested payload used by full-detail reads and wellbeing summaries:

- `Needs`

Where each `CachedNeed` contains:

- `Id`
- `Group`
- `Points`
- `Wellbeing`
- `Favorable`
- `Critical`
- `Active`

#### Fields that should stay out of the published read model

These are main-thread cache maintenance fields:

- `Go`
- `NeedMgr`
- `WbTracker`
- `Worker`
- `Life`
- `Carrier`
- `Deteriorable`
- `Contaminable`
- `Dweller`
- `Citizen`
- `LastWorkplaceRef`
- `LastDistrictRef`

This split keeps almost the entire beaver read model as a flat snapshot record. The only heavy nested payload is `Needs`, which makes beavers a strong candidate for a leaner projection-based design.

### Suggested Timberbot natural-resource projection split

`CachedNaturalResource` is already close to the desired projection shape.

#### `NaturalResourceDefinition`

Stable identity/classification data:

- `Id`
- `Name`
- `Cuttable`
- `Gatherable`

If classification should be fully detached from live component refs, these can become flat booleans such as:

- `IsCuttable`
- `IsGatherable`

#### `NaturalResourceState`

Periodic snapshot data:

- `X`, `Y`, `Z`
- `Alive`
- `Grown`
- `Growth`
- `Marked`

#### Fields that should stay out of the published read model

These are main-thread cache maintenance fields:

- `BlockObject`
- `Living`
- `Cuttable`
- `Gatherable`
- `Growable`

Unlike buildings and beavers, natural resources do not need a separate heavy detail payload. Their public read model can be almost entirely a flat snapshot record with a very small identity/classification layer.

## Working rule

- If the type is explicitly `ThreadSafe*`, it is the primary candidate for off-thread reads.
- If the data is published by Timberbot as a snapshot, it is safe to read off-thread.
- If the type is a normal Timberborn gameplay service or live component graph, keep it on the main thread.

## Implication for Timberbot

Safe to keep lean and off-thread:

- water
- terrain
- tile geometry queries built on `IThreadSafeWaterMap` and `IThreadSafeColumnTerrainMap`
- Timberbot snapshot reads

Still needs snapshotting or main-thread execution:

- buildings
- beavers
- districts
- science
- distribution
- notifications
- need/wellbeing data from live services
- anything that touches live entities or registries

## Revalidation workflow

When adding a new endpoint or trying to remove snapshotting:

1. Inspect the service with `/api/debug target:fields`.
2. Look for explicit `ThreadSafe*` types or internal thread-safe backing arrays.
3. Probe the exact method you want to use.
4. Only then decide whether the call can run off-thread or belongs in the snapshot path.
