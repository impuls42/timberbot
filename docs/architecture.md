# Architecture

> **v0.9 — architecture rework, in flight.** Behavior on `master` may briefly lag this page while units #13–#18 land. The shape described here is what ships when the rework is complete.

How Timberbot works internally. For migration history, see [`fresh-on-request-snapshots.md`](fresh-on-request-snapshots.md).

## The mod ↔ connector split

Timberbot is two cooperating processes:

- The **mod** runs inside the game and exposes the HTTP API. It is a pure server — it never spawns the agent. It owns the canonical session state (`mode`, `goal`, `pendingRequest`, `ready`, …) and a ready gate that refuses traffic until the player presses Launch.
- The **agent connector** (`tbot watch`) is a long-running Python process on the player's machine. It polls the mod, optionally registers a webhook URL for push triggers, sends heartbeats, and dispatches agent runs.

```
┌─ Timberborn (game process) ────────────────┐        ┌─ tbot watch (host process) ────────────┐
│  Timberbot.dll                             │        │                                        │
│    TimberbotHttpServer  :8085              │◀──────▶│  reconnect loop                        │
│    TimberbotAgentState  state.json         │   HTTP │  POST /api/tbot/heartbeat   every 2s   │
│    TimberbotReadV2 / TimberbotWrite        │        │  POST /api/tbot/register    on connect │
│    TimberbotWebhook                        │──push─▶│  webhook receiver (optional)           │
│    TimberbotPanel  (Launch / Stop widget)  │        │  agent run dispatcher                  │
└────────────────────────────────────────────┘        └────────────────────────────────────────┘
```

## Components

The mod has one read stack and one write stack:

- read: `TimberbotReadV2` — all GET endpoints, projection snapshots
- write: `TimberbotWrite` — all POST mutations
- entity lookup: `TimberbotEntityRegistry` — GUID/numeric ID bridge
- placement: `TimberbotPlacement` — building placement, A* path routing
- HTTP: `TimberbotHttpServer` — background listener, routing, ready-gate + auth middleware
- webhooks: `TimberbotWebhook` — batched push notifications
- debug: `TimberbotDebug` — reflection inspector, benchmark
- agent state: `TimberbotAgentState` — single source of truth for `mode`, `goal`, `ready`, `pendingRequest`, `tbotWebhookUrl`, `lastAckedRequestId`, `lastError`. Persisted fields live in `state.json`; ephemeral fields reset on save load.
- UI: `TimberbotPanel` — movable in-game widget (Launch / Stop, mode dropdown, mode-aware textarea) + centered settings modal
- orchestrator: `TimberbotService` — lifecycle, settings, per-frame dispatch
- write jobs: `ITimberbotWriteJob` — budgeted write execution

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

`TimberbotService` is the singleton orchestrator.

It owns:

- settings load from `settings.json` and state load from `state.json`
- cached settings state and debounced writeback to `settings.json`
- HTTP server lifetime
- event bus registration
- `Registry.BuildAllIndexes()`
- `ReadV2.BuildAll()`
- the shared `TimberbotAgentState` instance exposed to the panel, the HTTP layer, and webhook dispatch
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

State behavior:

- `Load()` reads `state.json` and seeds `mode`, `goal`, `lastError`. Ephemeral fields (`ready`, `pendingRequest`, `tbotWebhookUrl`, `lastAckedRequestId`) reset on every save load — the player must press Launch again after reloading a save.
- `Unload()` flushes `state.json` before shutting down the HTTP server.
- The service does not drive the agent itself; the connector does. The service just keeps state coherent.

## TimberbotAgentState — ready gate, modes, request slot

`TimberbotAgentState` is the thread-safe container that every other component reads and mutates.

Persisted fields (written to `state.json`):

- `mode` — `request` (default) or `autonomous`
- `goal` — autonomous-mode prompt
- `lastError` — last connector-reported failure surfaced in the widget

Ephemeral fields (reset on save load):

- `ready` — true after the player presses **Launch**; false on Stop or save load
- `pendingRequest` — single-slot `{id, prompt, createdAt}` set by `POST /api/agent/request` and cleared when the connector acks (`acked_request_id ≥ pendingRequest.id`)
- `tbotWebhookUrl` — connector's push URL from `POST /api/tbot/register`; cleared if heartbeats lapse > 6 s
- `lastAckedRequestId` — for ack/clear bookkeeping; this is the same value the connector sends as `acked_request_id` in the heartbeat payload (snake_case on the wire, PascalCase in the C# field)

### Ready gate

`TimberbotHttpServer` runs ready-gate middleware on every `/api/*` request. When `ready == false`, **both reads and writes** outside the carve-out return `409 {"error":"game_not_ready","hint":"player must press Launch in the Timberbot widget"}`.

Carve-out (always live):

- `/api/agent/*` — widget config + Launch trigger
- `/api/ready` — Launch / Stop toggle
- `/api/tbot/*` — connector heartbeat, register
- `/api/ping` — liveness probe

This is intentional. The gate is what makes the player the boss of the AI: nothing reads colony state, nothing places a building, nothing writes a save until the player explicitly opts in.

### Modes

- **Request mode** (default). Player types a prompt in the widget and presses Launch. The mod sets `pendingRequest` + fires the registered connector webhook (fast path). If the connector isn't reachable, it picks the request up via its next heartbeat (slow path).
- **Autonomous mode**. Connector decides cadence using the persisted `goal`. The ready gate is still authoritative — pressing Stop instantly mutes the connector.

The connector advances `acked_request_id` after each cycle so the mod can clear the single pending slot. Queueing is the connector's problem, not the mod's.

### Bearer-token auth

When `authToken` is set in `settings.json`, every `/api/*` route requires `Authorization: Bearer <token>` (constant-time compare). The mod **refuses to start** if `listenAddress` is non-localhost and `authToken` is empty — there's no path to ship the API over a LAN without a token. Tokens flow into the Python client via `[client].auth_token` in `config.toml`, the `TBOT_AUTH_TOKEN` env var, or `TimberbotClient(auth_token=...)`.

## TimberbotPanel

`TimberbotPanel` is the in-game control surface.

It owns:

- a movable bottom-right widget with a connection-state pill (`Disconnected` / `Not Ready` / `Idle` / `Running` / `Error`), a mode dropdown, a mode-aware textarea, and the **Launch / Stop** button
- a centered `Timberbot API - Settings` modal for runtime + security settings
- saved widget position via `widgetLeft` / `widgetTop`

UI model:

- the corner widget is always visible once loaded
- `Launch` posts `{"ready": true}` to `/api/ready`. `Stop` posts `{"ready": false}`.
- the mode dropdown writes `mode` via `POST /api/agent/config`
- in **Autonomous** mode the textarea is bound to `goal` with debounced auto-save
- in **Request** mode the textarea is a local buffer; pressing Launch posts the prompt to `/api/agent/request` and clears the field
- a banner ("Connected to game session — waiting for player to Launch") appears when a connector is heartbeating but the gate is off

The panel never spawns or knows about agent processes. It only talks to the local HTTP API.

## TimberbotReadV2

`TimberbotReadV2` is the read service for all GET endpoints.

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

`TimberbotEntityRegistry` is the entity lookup and ID translation layer.

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

`TimberbotWrite` handles all mutations on the main thread.

Write flow:

- HTTP listener parses request body (background thread)
- request queued to `ConcurrentQueue`
- `DrainRequests()` dequeues on Unity main thread
- write resolves numeric `id` through `TimberbotEntityRegistry`
- mutation runs against live game services/components

## TimberbotPlacement

`TimberbotPlacement` handles:

- `find_placement`. search region for valid building spots with reachability/power/flood scoring
- `place_building`. origin-correct, validate via `PreviewFactory`, place via `BlockObjectPlacerService`
- `demolish_building` / `demolish_crop`
- `route_path`. A* pathfinding with auto-stairs across z-levels, budgeted execution via `RoutePathJob`
- `collect_prefabs`. list building templates

## TimberbotWebhook

`TimberbotWebhook` batches event pushes and sends them out-of-band.

- events accumulate on the main thread via `[OnEvent]` handlers
- `FlushWebhooks()` sends batches on a configurable cadence (default 200ms)
- dispatch via `ThreadPool` (non-blocking)
- circuit breaker: N consecutive failures disables the webhook

Settings: `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`.

**Connector trigger channel.** When the connector calls `POST /api/tbot/register {webhook_url}`, the mod stores that URL in `tbotWebhookUrl`. On Launch in request mode, the mod fires a synthetic `agent.request` event at that URL as the fast path. Regular game-event webhooks still fire while `ready=false` — the ready gate only filters `/api/*` requests, not outbound webhooks. The connector registration is cleared if heartbeats lapse for 6 s.

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

Agent control is HTTP-only — the mod never spawns a process. The widget and the connector both speak `TimberbotHttpServer`:

| Route | Caller | Purpose |
|---|---|---|
| `GET /api/agent/state` | widget, connector | Read `{mode, goal, ready, pendingRequest, agentStatus, lastError}` |
| `POST /api/agent/config` | widget | Debounced save of `mode` and/or `goal` |
| `POST /api/agent/request` | widget (Launch in request mode) | Set `pendingRequest`; fire `tbotWebhookUrl` if registered |
| `POST /api/ready` | widget | Launch / Stop the ready gate |
| `POST /api/tbot/register` | connector | Register a webhook URL for push-mode triggering |
| `POST /api/tbot/heartbeat` | connector | 2 s liveness ping with `{version, agent_status, acked_request_id}`; returns full state |

The connector (`tbot watch`) owns the agent process lifetime. It dispatches `tbot agent run` (or `opencode run --attach <url>`) on each trigger, then advances `acked_request_id` so the mod can clear the pending slot.

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
  "listenAddress": "127.0.0.1",
  "authToken": "",
  "webhooksEnabled": true,
  "webhookBatchMs": 200,
  "webhookCircuitBreaker": 30,
  "webhookMaxPendingEvents": 1000,
  "webhookValidateUrls": true,
  "writeBudgetMs": 1.0,
  "maxBodyBytes": 1048576,
  "widgetLeft": "123",
  "widgetTop": "456"
}
```

There are three categories of settings:

- runtime, read by `TimberbotService`: `debugEndpointEnabled`, `httpPort`, `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `writeBudgetMs`
- security, also applied at load:
  - `listenAddress` — bind address; default `127.0.0.1`. Use `+`/`0.0.0.0` for LAN
  - `authToken` — bearer token required on every `/api/*` request when set. **Required** if `listenAddress` is non-localhost; the mod refuses to start otherwise.
  - `webhookValidateUrls` — reject SSRF-shaped webhook targets before dispatch; default `true`
  - `maxBodyBytes` — POST body size cap before `413 body_too_large`; default `1048576`
- widget position written by `TimberbotPanel`: `widgetLeft`, `widgetTop`

Agent-shaped state lives in **`state.json`** alongside `settings.json`, not in settings:

```json
{
  "mode": "request",
  "goal": "reach 50 beavers with 77 wellbeing",
  "lastError": null
}
```

Deprecated `settings.json` keys (`terminal`, `pythonCommand`, `agentBinary`, `agentGoal`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`, `tbotCommand`) are logged once at load and otherwise ignored. Backend choice, model, effort, and custom command templates live in `~/.config/timberbot/config.toml` (consumed by `tbot watch` and `tbot agent run`).

Important behavior:

- runtime/security settings are applied on load; changing them in the modal updates `settings.json` immediately in memory but may require reloading the save/mod to fully apply
- `state.json` is rewritten whenever the widget changes `mode`/`goal` or the connector reports a `lastError` (debounced)

## Path resolution (Python side)

The Python `tbot` CLI discovers Timberborn's `Documents` folder via `timberbot.paths.find_documents_dir`:

1. `$TBOT_DOCUMENTS_DIR` env var if set.
2. `~/Documents/Timberborn` if it exists (Windows / macOS / native Linux).
3. On Linux only: scan `~/.steam/steam/steamapps/compatdata/*/pfx/drive_c/users/steamuser/{My ,}Documents/Timberborn`, preferring the Timberborn Steam AppID `1062090`.
4. Otherwise raise `TimberbotPathError`.

CLI flags `--documents-dir=PATH` and `--mod-dir=PATH` override the resolved
value at call time.

## Test posture

Primary live harness: `python/tests/integration/v2_runner.py`. Validates the `/api/*` surface against a running game.

Modes: `smoke`, `freshness`, `write_to_read`, `performance`, `concurrency`, `all`. Invoke via `python -m pytest python/tests/integration/ -m integration` with `-k <mode>` to filter.

The connector and webhook receiver (`tbot watch`, `tbot listen`) are unit-tested with `pytest-httpserver` stubs — they don't require a live game.

## Known debt

- `/api/debug` and benchmark surfaces are evolving
- capture budgeting is intentionally conservative and may need tuning per domain
- `BuildAlertsFromBuildings` and `BuildPowerFromBuildings` still allocate `.ToArray()` per call (1 array each)

## Related docs

- [`fresh-on-request-snapshots.md`](fresh-on-request-snapshots.md). migration rationale and validation history
- [`thread-safe-surfaces.md`](thread-safe-surfaces.md). Timberborn thread-safety guidance
- [`developing.md`](developing.md). build, test, file structure
- [`performance.md`](performance.md). allocation audit, benchmarks, open issues
