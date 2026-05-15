# Architecture

How Timberbot works internally. For migration history, see [`fresh-on-request-snapshots.md`](fresh-on-request-snapshots.md).

## Components

The mod has one read stack and one write stack:

- read: [`TimberbotReadV2`](../timberbot/src/TimberbotReadV2.cs). all GET endpoints, projection snapshots
- write: [`TimberbotWrite`](../timberbot/src/TimberbotWrite.cs). all POST mutations
- entity lookup: [`TimberbotEntityRegistry`](../timberbot/src/TimberbotEntityRegistry.cs). GUID/numeric ID bridge
- placement: [`TimberbotPlacement`](../timberbot/src/TimberbotPlacement.cs). building placement, A* path routing
- HTTP: [`TimberbotHttpServer`](../timberbot/src/TimberbotHttpServer.cs). background listener, routing
- webhooks: [`TimberbotWebhook`](../timberbot/src/TimberbotWebhook.cs). batched push notifications
- debug: [`TimberbotDebug`](../timberbot/src/TimberbotDebug.cs). reflection inspector, benchmark
- agent: [`TimberbotAgent`](../timberbot/src/TimberbotAgent.cs). interactive Claude/Codex/custom-binary launcher
- UI: [`TimberbotPanel`](../timberbot/src/TimberbotPanel.cs). movable in-game widget + centered settings modal
- orchestrator: [`TimberbotService`](../timberbot/src/TimberbotService.cs). lifecycle, settings, per-frame dispatch
- write jobs: [`ITimberbotWriteJob`](../timberbot/src/ITimberbotWriteJob.cs). budgeted write execution

## Thread model

```
MAIN THREAD (Unity)                         BACKGROUND THREAD (HttpListener)
========================                    ================================
UpdateSingleton() [every frame]             ListenLoop() [blocking accept]
  |                                           |
  +-- DrainRequests() [POST only]             +-- GET request arrives
  |     |                                     |     |
  |     +-- RouteRequest() mutates game       |     +-- RouteRequest()
  |     +-- Respond() sends JSON              |     +-- ReadV2 serves from:
  |                                           |           - published snapshots, or
  +-- ReadV2.ProcessPendingRefresh()          |           - explicit thread-safe services
  |     |                                     |     +-- TimberbotJw serialization
  |     +-- advance main-thread capture       |     +-- Respond() sends JSON
  |     +-- queue background finalize/publish |
  |                                           +-- POST request arrives
  +-- ProcessWriteJobs() [budgeted]                 +-- queue to _pending
  |     |
  |     +-- step pending write jobs (2ms budget)
  |
  +-- FlushWebhooks() [every frame]
        |
        +-- batch _pendingEvents -> ThreadPool POST
```

| Location | Thread | Blocks game? |
|---|---|---|
| HTTP listener accept/GET response | background | no |
| GET endpoints | background | no |
| POST endpoints | main thread via `DrainRequests()` | yes, for duration |
| `ReadV2.ProcessPendingRefresh()` | main thread | yes, bounded by capture budget |
| `ProcessWriteJobs()` | main thread | yes, bounded by `writeBudgetMs` (default 1ms) |
| Webhook flush scheduling | main thread | negligible |

## TimberbotService

[`TimberbotService`](../timberbot/src/TimberbotService.cs) is the singleton orchestrator.

It owns:

- settings load from `settings.json`
- cached settings state and debounced writeback to `settings.json`
- HTTP server lifetime
- event bus registration
- `Registry.BuildAllIndexes()`
- `ReadV2.BuildAll()`
- `TimberbotAgent` creation and shutdown
- per-frame dispatch:
  - `DrainRequests()`
  - `ReadV2.ProcessPendingRefresh(now)`
  - `_server.ProcessWriteJobs(now, writeBudgetMs)`
  - `WebhookMgr.FlushWebhooks(now)`
  - `FlushSettingsIfNeeded(now)`

Settings behavior:

- runtime settings are loaded once in `Load()`
- the in-game settings UI mutates an in-memory `JObject`
- writes to disk are debounced (~1 second after the last change)
- `Unload()` forces a final flush

Agent ownership:

- `Load()` instantiates `Agent = new TimberbotAgent(_tbotCommand)`
- `Unload()` calls `Agent?.Stop()` before shutting down the HTTP server
- the service does not run the agent logic itself; it owns the single agent instance and exposes it to the panel and HTTP routes

## TimberbotAgent

[`TimberbotAgent`](../timberbot/src/TimberbotAgent.cs) is a thin in-process
wrapper around the Python `tbot agent run` CLI.

It owns:

- agent launch configuration: `binary` (backend name), `model`, `effort`,
  `goal`, optional custom command template, and process timeout
- agent state machine: `Idle`, `GatheringState`, `Interactive`, `Done`, `Error`
- a background worker thread for session startup and process waiting
- the currently launched process handle used by `Stop()`

Launch flow:

1. `Start(...)` validates the agent is not already running and stores the launch settings.
2. The agent enters `GatheringState` and starts a background thread.
3. That thread builds `tbot agent run --backend <name> --goal "<goal>"`
   (model/effort/command/terminal-prefix appended if set).
4. The process is spawned via `ProcessStartInfo { UseShellExecute = true }`,
   inheriting the user's shell so the agent CLI opens in its own terminal.
5. While the process is running, the agent reports `Interactive`.
6. When the process exits, the agent transitions to `Done`, or back to `Idle`
   if `Stop()` requested cancellation.

Prompt loading, instructions merging, platform-specific terminal wrapping, and
the actual agent-CLI invocation all live in the Python `tbot` package, not
here. PR 4 removed the legacy C# terminal handling, the Python launcher
auto-detection, and the in-process binary allowlist; argparse on the Python
side validates `--backend` against the registered backends.

The built-in agent is interactive, not an autonomous multi-turn executor. It
prepares context and launches the external CLI for the player to drive.

## TimberbotPanel

[`TimberbotPanel`](../timberbot/src/TimberbotPanel.cs) is the in-game control surface.

It owns:

- a movable bottom-right widget with status, `Start`, `Stop`, and `Settings`
- a centered `Timberbot API - Settings` modal
- agent launch settings: `agentBinary` (backend choice) and `agentGoal`
- runtime settings editing for the same `settings.json` file
- preset popups for the backend choice and boolean runtime fields
- custom row-hover tooltips inside the settings modal
- saved widget position via `widgetLeft` / `widgetTop`

UI model:

- the corner widget is always visible once loaded
- `Settings` opens the centered modal; the widget remains available underneath
- the Agent tab is intentionally minimal: Backend + Goal + Start. Model,
  effort, and custom command templates live in `~/.config/timberbot/config.toml`
  (consumed by the Python backends) rather than per-launch UI fields
- `Start` and `Stop` operate on the shared `TimberbotAgent` owned by `TimberbotService`

## TimberbotReadV2

[`TimberbotReadV2`](../timberbot/src/TimberbotReadV2.cs) is the read service for all GET endpoints.

It owns:

- fresh-on-request projection snapshots for entity collections
- value stores for singleton/aggregate endpoints
- collection/value/paged route helpers
- native serialization via `TimberbotJw`
- a private background finalize thread for snapshot publish work
- direct use of explicit Timberborn thread-safe services (terrain, water, soil)
- field-level reusable collections for derived endpoints (clusters, tiles, alerts, power, wellbeing)

Thread safety rule:

- listener-thread reads must come from published DTO snapshots or explicitly thread-safe game services
- listener-thread code must not walk live Timberborn entity/component graphs

## TimberbotEntityRegistry

[`TimberbotEntityRegistry`](../timberbot/src/TimberbotEntityRegistry.cs) is the entity lookup and ID translation layer.

It owns:

- entity lifecycle tracking via Timberborn `EventBus`
- GUID-backed identity mapping over Timberborn `EntityRegistry`
- `FindEntity(...)` for writes and placement
- shared constants (faction suffix, species lists, priority names)

Identity model:

- canonical internal key: Timberborn `EntityComponent.EntityId` (`Guid`)
- public API key: numeric `id` (Unity `GameObject.GetInstanceID()`)
- mapping: `int <-> Guid` in both directions

The public API uses short numeric IDs for human usability. The registry translates to GUIDs internally.

## TimberbotWrite

[`TimberbotWrite`](../timberbot/src/TimberbotWrite.cs) handles all mutations on the main thread.

Write flow:

- HTTP listener parses request body (background thread)
- request queued to `ConcurrentQueue`
- `DrainRequests()` dequeues on Unity main thread
- write resolves numeric `id` through `TimberbotEntityRegistry`
- mutation runs against live game services/components

## TimberbotPlacement

[`TimberbotPlacement`](../timberbot/src/TimberbotPlacement.cs) handles:

- `find_placement`. search region for valid building spots with reachability/power/flood scoring
- `place_building`. origin-correct, validate via `PreviewFactory`, place via `BlockObjectPlacerService`
- `demolish_building` / `demolish_crop`
- `route_path`. A* pathfinding with auto-stairs across z-levels, budgeted execution via `RoutePathJob`
- `collect_prefabs`. list building templates

## TimberbotWebhook

[`TimberbotWebhook`](../timberbot/src/TimberbotWebhook.cs) batches event pushes and sends them out-of-band.

- events accumulate on the main thread via `[OnEvent]` handlers
- `FlushWebhooks()` sends batches on a configurable cadence (default 200ms)
- dispatch via `ThreadPool` (non-blocking)
- circuit breaker: N consecutive failures disables the webhook

Settings: `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`.

## Read architecture

There are three read patterns inside `ReadV2`.

### 1. Projection-backed collections

Used for entity-style endpoints: `buildings`, `beavers`, `trees`, `crops`, `gatherables`.

Shape:

- main-thread tracked refs (added/removed via `EventBus` lifecycle events)
- `ProjectionSnapshot<TDef, TState, TDetail>` with double-buffered capture arrays
- main thread captures live state into DTO buffers under a per-frame budget (~1ms)
- background finalize thread publishes immutable snapshots
- `CollectionRoute` handles format/pagination/filtering/serialization from published data
- concurrent readers coalesce onto shared publishes

### 2. Value stores

Used for singleton endpoints: `settlement`, `time`, `weather`, `speed`, `workhours`, `science`, `distribution`.

Shape:

- `ValueStore<TCapture, TSnapshot>` with capture/finalize/publish pipeline
- main-thread capture produces a typed DTO
- background finalize converts to published snapshot where useful
- `ValueRoute` handles serialization from published data

### 3. Derived reads

Used for aggregate endpoints: `summary`, `alerts`, `power`, `wellbeing`, `districts`, `resources`, `population`, `tree_clusters`, `food_clusters`.

Built from published snapshots and explicit thread-safe surfaces. Use field-level reusable collections (dicts, lists, arrays) that are cleared-in-place per request for zero steady-state allocation.

## Fresh-on-request behavior

The read contract:

- a GET may wait across one or more frames for the next publish
- the returned data is fresh as of the frame that serviced the request
- concurrent readers coalesce onto shared publishes
- there is no cadence-driven refresh. snapshots publish only when readers need them
- `ProcessPendingRefresh()` is a bounded capture scheduler, not a periodic loop
- expensive finalize/publish work runs on `ReadV2`'s dedicated background thread

## ID model

### Public ID

- numeric `id` (Unity `GameObject.GetInstanceID()`)
- exposed in GET payloads, accepted by write endpoints
- easy for humans and scripts to type

### Internal ID

- `Guid` (Timberborn `EntityComponent.EntityId`)
- used for canonical identity and bridging into Timberborn `EntityRegistry`

Compatibility mapping lives in `TimberbotEntityRegistry`: `int <-> Guid`.

## Request flow

### GET

```
HTTP GET
  -> ListenLoop() [background thread]
  -> RouteRequest()
  -> ReadV2 endpoint method
     -> request fresh snapshot/value if needed
     -> wait for publish if needed
     -> filter/paginate/serialize from published data
  -> Respond()
```

### POST

```
HTTP POST
  -> ListenLoop() [background thread]
  -> parse JSON body
  -> enqueue PendingRequest
  -> [next frame] DrainRequests() [main thread]
  -> RouteRequest()
  -> Write/Placement/Webhook mutation
  -> Respond()
```

### Agent control

Agent control is split across GET and queued POST routes in [`TimberbotHttpServer`](../timberbot/src/TimberbotHttpServer.cs):

- `GET /api/agent/status` returns the current `TimberbotAgent.Status()` payload
- `POST /api/agent/start` is queued and calls `Agent.Start(binary, model, effort, timeout, goal)` on the main-thread write path
- `POST /api/agent/stop` is queued and calls `Agent.Stop()`

The in-game panel uses the same shared `Agent` instance as the HTTP routes.

## Serialization

`TimberbotJw` is the core JSON writer. Zero-alloc fluent API that writes directly to a reusable `StringBuilder`.

Usage pattern:

- each request/build path owns its own writer instance
- `Reset()` per request
- staged finalize paths avoid reusing main-thread writers across threads

Major writers: `ReadV2` (main + science/distribution builders), `Write`, `Placement`, `Webhook`, `HttpServer` (error responses).

## Spatial reads

`/api/tiles` reads from:

- `IThreadSafeWaterMap`. water depth and contamination
- `IThreadSafeColumnTerrainMap`. terrain height
- safe-wrapped `ISoilContaminationService` / `ISoilMoistureService`
- published building/resource snapshots for occupant data
- field-level reusable occupant lists (cleared-in-place per request)

## Data freshness

### Fresh-on-request

Projection snapshots and value stores. Request-triggered, waits for publish, best freshness guarantee.

### Event-driven

Registry data (GUID-to-ID maps, webhook lifecycle hooks). Updated on `EntityInitializedEvent`/`EntityDeletedEvent`. Used for entity lookup and compatibility only.

## Settings

`settings.json` in the mod folder:

```json
{
  "debugEndpointEnabled": true,
  "httpPort": 8085,
  "listenAddress": "localhost",
  "webhooksEnabled": true,
  "webhookBatchMs": 200,
  "webhookCircuitBreaker": 30,
  "webhookMaxPendingEvents": 1000,
  "webhookValidateUrls": true,
  "writeBudgetMs": 1.0,
  "maxBodyBytes": 1048576,
  "agentBinary": "claude",
  "agentGoal": "reach 50 beavers with 77 well-being",
  "widgetLeft": "123",
  "widgetTop": "456"
}
```

There are three categories of settings in the same file:

- runtime settings read by [`TimberbotService`](../timberbot/src/TimberbotService.cs):
  - `debugEndpointEnabled`
  - `httpPort`
  - `webhooksEnabled`
  - `webhookBatchMs`
  - `webhookCircuitBreaker`
  - `webhookMaxPendingEvents`
  - `writeBudgetMs`
- security settings (also read by `TimberbotService`, applied at load):
  - `listenAddress` — bind address; default `localhost`. Use `+`/`0.0.0.0` for LAN
  - `webhookValidateUrls` — reject SSRF-shaped webhook targets before dispatch; default `true`
  - `maxBodyBytes` — POST body size cap before `413 body_too_large`; default `1048576`
- UI/agent settings written by [`TimberbotPanel`](../timberbot/src/TimberbotPanel.cs):
  - `agentBinary` — backend name (claude/codex/opencode/custom)
  - `agentGoal`
  - `widgetLeft`
  - `widgetTop`

Deprecated keys (`terminal`, `pythonCommand`, `agentModel`, `agentEffort`,
`agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`) are
logged once at load and otherwise ignored — see
[`TimberbotPure.DetectDeprecatedSettings`](../timberbot/src/TimberbotPure.cs).
Per-backend model/effort/command defaults live in
`~/.config/timberbot/config.toml` (consumed by the Python `tbot` CLI).

Important behavior:

- runtime settings are applied on load; changing them in the modal updates `settings.json` immediately in memory but may require reloading the save/mod to fully apply
- UI/agent settings are consumed live by the panel and agent launcher

## Path resolution (Python side)

The Python `tbot` CLI discovers Timberborn's `Documents` folder via
[`timberbot.paths.find_documents_dir`](../python/src/timberbot/paths.py):

1. `$TBOT_DOCUMENTS_DIR` env var if set.
2. `~/Documents/Timberborn` if it exists (Windows / macOS / native Linux).
3. On Linux only: scan `~/.steam/steam/steamapps/compatdata/*/pfx/drive_c/users/steamuser/{My ,}Documents/Timberborn`, preferring the Timberborn Steam AppID `1062090`.
4. Otherwise raise `TimberbotPathError`.

CLI flags `--documents-dir=PATH` and `--mod-dir=PATH` override the resolved
value at call time.

## Test posture

Primary live harness: [`python/tests/integration/v2_runner.py`](../python/tests/integration/v2_runner.py). Validates the `/api/*` surface against a running game.

Modes: `smoke`, `freshness`, `write_to_read`, `performance`, `concurrency`, `all`. Invoke via `python -m pytest python/tests/integration/ -m integration` with `-k <mode>` to filter.

## Known debt

- `/api/debug` and benchmark surfaces are evolving
- capture budgeting is intentionally conservative and may need tuning per domain
- `BuildAlertsFromBuildings` and `BuildPowerFromBuildings` still allocate `.ToArray()` per call (1 array each)

## Related docs

- [`fresh-on-request-snapshots.md`](fresh-on-request-snapshots.md). migration rationale and validation history
- [`thread-safe-surfaces.md`](thread-safe-surfaces.md). Timberborn thread-safety guidance
- [`developing.md`](developing.md). build, test, file structure
- [`performance.md`](performance.md). allocation audit, benchmarks, open issues
