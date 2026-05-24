# Fresh-on-Request Snapshots

> **Status:** historical — migration complete. The fresh-on-request architecture is now the live implementation. See [`../docs/architecture.md`](../docs/architecture.md) for the current design.

Historical design document for the migration from cadence-driven double buffer to demand-driven published snapshots.

Related:

- [`../docs/architecture.md`](../docs/architecture.md). current architecture
- [`thread-safe-surfaces.md`](thread-safe-surfaces.md). what Timberborn exposes as actually thread-safe
- [`../docs/performance.md`](../docs/performance.md). benchmark baseline

## Current status

The fresh-on-request read path is no longer just a design spike. It is now the native implementation behind the canonical `/api/*` surface.

Implemented:

- canonical `/api/*` GET routing
- `TimberbotReadV2` as the single v2 read service
- generic collection, value, paging, and snapshot helpers folded into `TimberbotReadV2.cs`
- removal of the temporary top-level helper files:
  - `TimberbotCollectionEndpoint.cs`
  - `TimberbotCollectionQuery.cs`
  - `TimberbotSnapshot.cs`
  - `TimberbotValueStore.cs`
  - `TimberbotValueEndpoint.cs`
  - `TimberbotPagedEndpoint.cs`
- fresh-on-request projection-backed reads for buildings, beavers, and natural-resource entity collections
- native v2 value and derived endpoints for summary, districts, resources, population, alerts, power, wellbeing, notifications, science, distribution, time, weather, speed, workhours, tree clusters, and food clusters
- staged refresh inside `TimberbotReadV2`: main-thread capture plus background finalize/publish
- a dedicated v2 experiment harness at `python/tests/integration/v2_runner.py`

Important implementation note:

- `TimberbotReadV2` no longer calls `_legacyRead.*`
- `/api/*` is now the only live read surface
- `TimberbotRead` and `/api/v1/*` were removed after parity + fixture capture

The new harness is intentionally separate from `test_validation.py` and covers:

- `smoke`
- `parity`
- `freshness`
- `performance`
- `concurrency`
- `all`

It writes three artifacts per run under `timberbot/test-results/v2/`:

- full transcript `.log`
- human-readable summary `.md`
- machine-readable `.json`

Console behavior is quiet-by-default:

- passing runs print nothing
- failing runs print only the failures and artifact paths

## Current validation

Current live status after build, reload, and retest:

- final `/api/v1/*` vs `/api/*` parity run before removal: `318 passed, 0 failed`
- post-cut `/api/*` full suite: `64 passed, 0 failed, 33 skipped`
- post-staged-refresh full suite: `72 passed, 0 failed, 29 skipped`

Important concrete progress:

- the stale legacy building-row bug was fixed
- `GET /api/buildings` matched legacy across the full supported matrix before `v1` removal
- `TimberbotReadV2` no longer contains any `_legacyRead.*` calls
- `TimberbotRead` has been deleted from the mod
- the generic v2 helpers are now internal to `TimberbotReadV2`, not separate top-level classes

Latest verified artifact files:

- [20260327-144244-smoke.md](/C:/code/timberborn/timberbot/test-results/v2/20260327-144244-smoke.md)
- [20260327-144247-parity.md](/C:/code/timberborn/timberbot/test-results/v2/20260327-144247-parity.md)

The stale-row bug mattered because the old cache was returning ghost `CachedBuilding` entries that no longer resolved through `Cache.FindEntity(id)`. The fix was to prune stale IDs from the legacy cache during refresh so parity is against live state instead of dead cached rows.

## Goal

Preserve the important property of the current design:

- normal GET endpoints do not read live Timberborn objects on the listener thread

while removing the part that feels wrong:

- continuous periodic cache refresh even when nobody is reading

Target behavior:

- a normal read request asks for fresh data
- the request waits for the next main-thread refresh for that entity type
- the refreshed data is published as an immutable/read-only snapshot
- the request then responds from that snapshot off-thread
- no migrated entity type performs continuous refresh work while idle

Current implementation note:

- the system no longer treats fresh-on-request as a single main-thread publish step
- `ReadV2.ProcessPendingRefresh()` now advances bounded main-thread capture work
- completed captures are finalized and published on an internal background worker
- requests can wait across multiple frames if capture spills under budget

## Core idea

Replace:

- one long-lived mirrored cache object graph that is refreshed on cadence

with:

- main-thread-only live trackers
- immutable/read-only published snapshots
- per-entity-type fresh-read coordination

This is a better fit for what Timberborn appears to do internally:

- use explicit thread-safe game surfaces where available
- use narrow thread-safe projections where needed
- keep normal live gameplay state on the main thread

## Endstate behavior

### Freshness contract

For projection-backed endpoints, a response is fresh **as of the main-thread frame that serviced the request**.

That means:

- requests do not return stale data just because the system was idle
- requests may wait up to one frame for the next publish
- multiple requests arriving before that publish share the same refresh result

This is intentionally not the old “return whatever was last published” behavior.

### Idle behavior

While there is no read demand for a migrated entity type:

- no periodic mutable-state refresh runs for that entity type

Structural tracking still remains event-driven:

- entity create/delete
- any index maintenance required to keep the tracker set valid

### Throughput behavior

The main thread performs **at most one refresh per frame per migrated entity type**.

This bounds worst-case gameplay cost under polling:

- many requests in one frame share one refresh
- the system does not refresh once per request

In the current staged implementation, a "refresh" is split into:

- main-thread capture from live Timberborn objects
- background finalize/publish from captured DTO buffers

That means the bound that matters is now:

- at most one active capture per migrated domain at a time
- publish work no longer has to finish on the same main-thread frame

## Architectural split

Every migrated entity type should have four layers.

### 1. `Tracked*Ref`

Main-thread-only live data used to gather state.

Responsibilities:

- hold component refs
- resolve refs once at add time
- participate in create/delete event handling
- never leave the main thread

These are not published to readers.

### 2. `*Definition`

Static or event-driven data.

Updated:

- when the entity is created
- when structural facts change

Examples:

- ID, name
- stable coordinates
- orientation
- type flags
- cached geometry/footprints

### 3. `*State`

Flat mutable runtime state.

Updated:

- only when fresh readers are waiting

Examples:

- paused/powered/workers for buildings
- wellbeing/position/carrying for beavers
- alive/grown/marked for natural resources

### 4. `*DetailState`

Heavy nested payloads needed only for full-detail reads.

Updated:

- only when a waiting request requires that detail level

Examples:

- building inventory/recipes/nutrients
- beaver needs

### Published snapshot

Each entity type gets one published snapshot object, for example:

- `PublishedBuildingsSnapshot`
- `PublishedBeaversSnapshot`
- `PublishedNaturalResourcesSnapshot`

Properties:

- immutable/read-only after publish
- contains only DTO data
- no live Timberborn refs
- atomically swapped by reference

## First migration spike

The first spike should be:

- `GET /api/buildings_v2`

Reason:

- it is the most complex current read model
- it carries the most nested data
- if the projection split works here, the beaver and natural-resource cases should be easier

### `buildings_v2` requirements

The new endpoint should match current `GET /api/buildings` behavior for:

- `format`
- `detail`
- `limit`
- `offset`
- `name`
- `x`, `y`, `radius`

Supported detail levels:

- `basic`
- `full`
- `id=<id>`

Schema compatibility requirement:

- same schema as current `buildings` output in both `toon` and `json`

The current `GET /api/buildings` remains in place during the spike.

## Building projection design

### `TrackedBuildingRef`

Main-thread-only live refs:

- entity/component refs used to gather mutable building state
- any refs needed for event-driven structural maintenance

This is the only place where building live refs should remain once the v2 path is implemented.

### `BuildingDefinition`

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

### `BuildingState`

Mutable flat snapshot data:

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

### `BuildingDetailState`

Nested full-detail data only:

- `Inventory`
- `Recipes`
- `NutrientStock`

### Not part of the published read model

Do not publish these off-thread:

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

## Fresh-read coordination

Each migrated entity type should have a small coordination object.

For buildings, responsibilities are:

- accept fresh-read requests from listener-thread GETs
- record the highest required detail level among waiting requests
- indicate to the main thread that a refresh is pending
- wake waiting requests after publish

### Request flow

For `GET /api/buildings_v2`:

1. Listener thread parses request
2. Listener thread registers a fresh-read request with the building coordinator
3. Listener thread waits
4. Main thread refreshes buildings on next frame
5. Main thread publishes new `PublishedBuildingsSnapshot`
6. Waiting requests wake
7. Listener thread filters/paginates/serializes from the published snapshot

### Current staged coordination model

The original single-phase design has now been replaced with a staged generic implementation inside `TimberbotReadV2`:

1. listener-thread request registers as a waiter
2. main thread marks capture pending for the required domain
3. `ProcessPendingRefresh(now)` advances capture up to the configured frame budget
4. when capture completes, `ReadV2` queues finalize work to its private background worker
5. finalize builds any thread-safe derived strings/JSON and publishes the immutable snapshot
6. only the waiters attached to that in-flight publish are released

Important correctness rule:

- waiters that arrive after capture has started are held for the next publish, not incorrectly released by the current one

### Coalescing rule

- one refresh per frame max
- all requests waiting during that frame share the same publish
- if any waiting request asks for `detail=full`, build detail data for that publish

### Timeout rule

If a request cannot be satisfied because the game is not advancing frames:

- wait up to 2 seconds
- respond `503 refresh_timeout`

Do not block forever.

## Main-thread frame order

For migrated fresh-read endpoints, `UpdateSingleton()` should conceptually run in this order:

1. Drain queued writes
2. Apply structural entity changes already captured by event handlers
3. Process pending fresh-read refreshes
4. Publish snapshots and release waiters
5. Flush webhooks

Current implementation detail:

- step 3 is now bounded capture scheduling
- step 4 may finish on the `ReadV2` finalize worker after capture completes
- the freshness contract is preserved by waiting for publish completion, not by forcing all work into one frame

This order matters because:

- a fresh read after a write should see the newest state
- snapshot publish should happen after write-side mutations for that frame

## Listener-thread rules

For migrated endpoints, the listener thread may:

- read the published snapshot
- filter
- paginate
- serialize

The listener thread may not:

- call Timberborn services
- touch live component refs
- read tracker objects
- call `GetComponent<T>()`
- traverse live entity graphs

## Debug and benchmark support

The spike should include enough observability to answer whether the design is working.

Expose or record:

- latest publish timestamp or sequence for each migrated entity type
- number of refreshes performed
- number of structural republishes
- whether detail payload was built for a publish
- item counts in the current published snapshot

This should go through existing debug/benchmark surfaces rather than inventing a second diagnostics system.

## Acceptance criteria

### Functional

- `buildings_v2 basic` matches `buildings basic`
- `buildings_v2 full` matches `buildings full`
- `id=<id>` behavior matches current endpoint
- pagination and filtering semantics match
- toon/json schema matches current endpoint exactly

### Freshness

- after a write or player action that changes building state, `buildings_v2` returns data refreshed on the servicing frame
- requests arriving in the same frame share one refresh

### Thread safety

- no live building data is read on the listener thread for `buildings_v2`
- published snapshots contain DTOs only

### Performance

Compared to current `buildings full`:

- no periodic idle refresh cost for buildings-v2
- no more than one building refresh per frame under active demand
- no material latency regression in the endpoint
- code complexity is reduced by removing published live refs and clone-driven buffer semantics from the read path

## V2 Validation Requirements

For every `/api/v2/*` endpoint, validation should happen in three layers.

### Parity

- compare v1 vs v2 JSON output
- compare v1 vs v2 TOON output
- compare filtered and paginated responses
- compare `detail=basic`, `detail=full`, and `id=<id>` where supported

For entity endpoints:

- full-list equality
- single-id sampled equality
- stable fingerprint logging on mismatch instead of dumping large payloads

### Freshness

For projection-backed endpoints:

- issue a write
- request the corresponding `/api/v2/*` endpoint immediately
- verify the response reflects the servicing frame

Required building scenarios:

- pause/unpause
- worker count change
- floodgate change
- placement/demolition
- recipe change
- clutch change

### Performance

Keep two benchmark layers:

- broad suite: `performance`
- focused suites per migrated entity group:
  - `building_endpoint_perf`
  - later `beaver_endpoint_perf`
  - later `natural_resource_endpoint_perf`

Acceptance target for a migrated entity group:

- 0 failed responses in a 200-iteration focused perf run
- average latency within 10% of legacy
- no persistent instability across repeated focused runs

### Concurrency

- burst concurrent requests to the same `/api/v2/*` endpoint should share publishes correctly
- no `refresh_timeout` should occur under normal frame progression
- detail and basic requests arriving in the same frame should share one publish, with detail included if any waiter requires it

## Current spike results

The buildings spike has been implemented and measured against the live game.

### Functional status

- `GET /api/buildings_v2` exists and is live
- `buildings_v2` basic matches legacy `buildings`
- `buildings_v2` full matches legacy `buildings`
- sampled `id=<id>` parity checks passed

Dedicated parity test:

```powershell
python -m pytest python/tests/integration/ -m integration -k buildings_v2_parity
```

Latest passing parity result file:

- [20260327-093637-buildings_v2_parity.txt](/C:/code/timberborn/timberbot/test-results/20260327-093637-buildings_v2_parity.txt)

### Performance status

There are now two relevant perf entry points:

- broad perf suite: `performance`
- building-only benchmark: `building_endpoint_perf`

Recommended command for this spike:

```powershell
python -m pytest python/tests/integration/ -m integration -k building_endpoint_perf
```

Latest stable 200-iteration result:

- [20260327-095537-building_endpoint_perf.txt](/C:/code/timberborn/timberbot/test-results/20260327-095537-building_endpoint_perf.txt)

| Endpoint | Avg | Min | Max | Success |
|---|---:|---:|---:|---:|
| `buildings` | 274 ms | 237 ms | 1267 ms | 200 / 200 |
| `buildings full` | 298 ms | 257 ms | 1269 ms | 200 / 200 |
| `buildings_v2` | 284 ms | 238 ms | 1291 ms | 200 / 200 |
| `buildings_v2 full` | 299 ms | 260 ms | 1248 ms | 200 / 200 |

Legacy vs v2 from that run:

| Scenario | Legacy | V2 | Delta | Ratio |
|---|---:|---:|---:|---:|
| basic | 274 ms | 284 ms | +10 ms | 1.04x |
| full | 298 ms | 299 ms | +1 ms | 1.00x |

Interpretation:

- request latency is effectively at parity
- the fresh-on-request model is not showing a meaningful regression for buildings
- the main architectural advantage is now freshness and idle-cost reduction, not raw speedup

### Earlier unstable run

One earlier 50-iteration run showed transient instability in `buildings_v2` basic:

- [20260327-094047-buildings_v2_performance.txt](/C:/code/timberborn/timberbot/test-results/20260327-094047-buildings_v2_performance.txt)

That run reported:

- `buildings_v2`: 42 / 50 successful, 635 ms avg, 3771 ms max
- `buildings_v2 full`: 50 / 50 successful, 252 ms avg

That specific failure mode has not reproduced in later isolated `building_endpoint_perf` runs:

- 50 iterations: [20260327-094915-building_endpoint_perf.txt](/C:/code/timberborn/timberbot/test-results/20260327-094915-building_endpoint_perf.txt)
- 200 iterations: [20260327-095537-building_endpoint_perf.txt](/C:/code/timberborn/timberbot/test-results/20260327-095537-building_endpoint_perf.txt)

Current conclusion:

- the spike currently looks viable
- there was at least one transient bad run, so burst/concurrency behavior still deserves investigation before declaring the architecture finished

## Generic V2 Architecture

The current implementation keeps the v2 architecture centered in `TimberbotReadV2`. The goal is not to spread v2 behavior across a growing set of top-level helper services. The goal is:

- keep `TimberbotRead` untouched while v2 is being proved
- move all new work to `/api/v2/*`
- keep one generic typed v2 read stack inside `TimberbotReadV2`
- migrate endpoint groups onto that stack without duplicating route mechanics
- delete the old stack only after v2 reaches full coverage and proven parity

Fixed migration rules:

- canonical new routes live under `/api/v2/*`
- ad-hoc routes like `/api/buildings_v2` are transitional only
- `TimberbotReadV2` owns the v2 implementation directly
- generic reuse happens through internal nested helpers and typed schemas, not extra public service classes
- old `/api/*` routes stay in place until `/api/v2/*` is complete and proven

## DRY Boundary

The generic boundary for v2 should be:

- snapshot lifecycle and waiter coordination
- entity tracking and structural dirtiness
- query parsing
- filtering and pagination
- list response assembly
- typed row serialization hooks

The generic boundary should not be:

- reflection-based schema discovery
- auto-generated serializers
- a giant magic endpoint framework that hides field-level behavior

V2 should stay typed and explicit, but the repeated mechanics should exist only once.

## Core V2 Types

### `ProjectionSnapshot<TDef, TState, TDetail>`

This is the generic fresh-on-request snapshot primitive used inside `TimberbotReadV2` for projection-backed entity domains.

Responsibilities:

- expose fresh-read waiting and published snapshot access
- expose publish metrics
- keep published DTO arrays for definitions, state, and detail
- coordinate staged capture vs finalize state
- keep waiter ownership correct across in-flight publishes

Required typed hooks:

- `GetDefinition(int index)`
- `RefreshState(TState state, int index)`
- `RefreshDetail(TDetail detail, int index)`

Rules:

- published snapshots contain only DTO data
- no reflection in hot paths
- no live Timberborn refs in published state
- any background finalize step may only touch captured DTO buffers, not live tracked refs

### `CollectionQuery`

Shared query model for all collection-style endpoints.

Fields:

- `Format`
- `Detail`
- `SingleId`
- `Limit`
- `Offset`
- `FilterName`
- `FilterX`
- `FilterY`
- `FilterRadius`
- `HasFilter`
- `Paginated`
- `NeedsFullDetail`

Required parser behavior:

- `id=<id>` implies full detail and disables the pagination wrapper
- `limit=0` means unlimited and returns a flat array
- name/radius filters apply before pagination
- paginated responses use `{total, offset, limit, items}`

### `CollectionRoute<TDef, TState, TDetail>`

Generic responder for entity-list endpoints.

Responsibilities:

- parse `CollectionQuery`
- request fresh or current snapshot from the store
- compute filtered totals
- paginate
- serialize rows
- return the same response shape as the legacy endpoint

This shared responder should own the list mechanics once so entity endpoints do not each re-implement:

- `singleId`
- `fullDetail`
- `hasFilter`
- `paginated`
- `total`
- `skipped`
- `emitted`
- wrapper handling

### `ICollectionSchema<TDef, TState, TDetail>`

Typed serialization contract for one endpoint.

Required members:

- `int GetId(TDef def)`
- `string GetName(TDef def)`
- `int GetX(TDef def, TState state)`
- `int GetY(TDef def, TState state)`
- `bool IncludeRow(TDef def, TState state)`
- `void WriteRow(TimberbotJw jw, string format, bool fullDetail, TDef def, TState state, TDetail detail)`

Rules:

- row writing stays explicit and typed
- the generic layer handles collection mechanics
- schemas handle only row content
- per-schema scratch buffers are allowed because serialization stays on the listener thread

### `ValueStore<TCapture, TSnapshot>` and `ValueRoute<TSnapshot>`

These are the matching native-v2 primitives for singleton or aggregate snapshots that do not need collection filtering.

In the current implementation they also support staged refresh:

- main-thread capture returns `TCapture`
- background finalize turns that into the published `TSnapshot`
- trivial endpoints may use the same type for both capture and published snapshot

They are used for endpoints such as:

- `settlement`
- `time`
- `weather`
- `speed`
- `workhours`
- `science`
- `distribution`
- `summary`

### `FlatArrayRoute<TItem>`

This is the matching route shape for endpoints that paginate a flat list without entity-style name/radius/detail handling.

It is used for endpoints such as:

- `alerts`
- `notifications`
- `districts`
- `resources`
- `population`
- `tree_clusters`
- `food_clusters`

## `TimberbotReadV2` Composition

`TimberbotReadV2` is now the only public v2 read service.

It owns:

- entity tracking and publish coordination
- generic route helpers as nested/private types
- typed schemas for each route shape
- endpoint methods that stay thin and map onto those shared helpers

It does not:

- call `_legacyRead`
- depend on extra top-level v2 helper services
- duplicate list parsing, paging, or response-wrapper mechanics per endpoint

Current native v2 endpoint ownership inside `TimberbotReadV2`:

- collection routes:
  - `buildings`
  - `beavers`
  - `trees`
  - `crops`
  - `gatherables`
- value routes:
  - `settlement`
  - `time`
  - `weather`
  - `speed`
  - `workhours`
  - `science`
  - `distribution`
  - `summary`
  - `wellbeing`
  - `power`
- flat-array routes:
  - `alerts`
  - `notifications`
  - `districts`
  - `resources`
  - `population`
  - `tree_clusters`
  - `food_clusters`

## `/api/v2/*` Coverage

### Native `TimberbotReadV2` endpoints

These are implemented natively inside `TimberbotReadV2` and validated through the v2 harness:

- `/api/v2/buildings`
- `/api/v2/beavers`
- `/api/v2/trees`
- `/api/v2/crops`
- `/api/v2/gatherables`
- `/api/v2/summary`
- `/api/v2/alerts`
- `/api/v2/power`
- `/api/v2/population`
- `/api/v2/wellbeing`
- `/api/v2/time`
- `/api/v2/weather`
- `/api/v2/speed`
- `/api/v2/workhours`
- `/api/v2/settlement`
- `/api/v2/science`
- `/api/v2/distribution`
- `/api/v2/notifications`
- `/api/v2/resources`
- `/api/v2/districts`
- `/api/v2/tree_clusters`
- `/api/v2/food_clusters`

### Direct route exceptions

These remain outside `TimberbotReadV2` for now:

- `/api/v2/ping`
- `/api/v2/prefabs`

## Implementation Sequence

Completed implementation sequence:

1. Introduced canonical `/api/v2/*` routing.
2. Kept legacy `/api/*` routes unchanged for parity.
3. Proved the fresh-on-request model on buildings.
4. Folded generic collection, value, paging, and snapshot helpers into `TimberbotReadV2`.
5. Migrated beavers and natural resources onto the same internal route patterns.
6. Rebuilt summary, alerts, power, wellbeing, districts, resources, population, and cluster endpoints as native v2 reads.
7. Removed `_legacyRead.*` usage from `TimberbotReadV2`.
8. Revalidated smoke and full parity after reload.
9. Split `ReadV2.ProcessPendingRefresh()` into bounded main-thread capture plus background finalize/publish.
10. Revalidated the full live suite after the staged refresh cutover.

## Remaining follow-on work

The remaining migration work is mostly outside the core v2 GET stack:

1. decide when `/api/*` should switch to the native v2 implementations
2. retire obsolete legacy-only code once the team is comfortable removing the old parity oracle
3. keep expanding freshness, performance, and concurrency coverage as write-path scenarios are hardened
4. tune capture budgeting and publish metrics now that the staged pipeline is live

## Explicit non-goals for the spike

The first spike should not:

- remove the old building endpoint
- migrate all entity types at once
- redesign water/terrain reads that are already backed by Timberborn `ThreadSafe*` services
- attempt a single giant world snapshot

The spike is only meant to prove that fresh-on-request published snapshots are the right replacement for cadence-driven mirrored caches.
