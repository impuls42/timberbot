# Architecture

> **v0.9 — WebSocket cutover, in flight.** Behavior on `master` may briefly lag this page while the WS rework lands. The shape described here is what ships when the rework is complete.

How Timberbot works internally. For migration history, see [`fresh-on-request-snapshots.md`](fresh-on-request-snapshots.md).

## The mod ↔ connector split

Timberbot is two cooperating processes:

- The **mod** runs inside the game and exposes an HTTP API on port 8085 and a parallel WebSocket on port 8086. It is a pure server — it never spawns the agent. It owns the canonical session state (`mode`, `goal`, `pendingRequest`, `ready`, …) and a ready gate that refuses HTTP traffic until the player presses Launch.
- The **agent connector** (`tbot watch`) is a long-running Python process on the player's machine. It opens a single long-lived WebSocket to the mod, receives state pushes and game events, and dispatches agent runs.

```
┌─ Timberborn (game process) ────────────────┐         ┌─ tbot watch (host process) ────────────┐
│  Timberbot.dll                             │         │                                        │
│    TimberbotHttpServer       :8085         │◀─HTTP──▶│  reads/writes via REST                 │
│    TimberbotWebSocketServer  :8086         │◀── WS ─▶│  long-lived ws://host:8086/api/ws      │
│    TimberbotAgentState  state.json         │         │  receives state + event frames         │
│    TimberbotReadV2 / TimberbotWrite        │         │  sends heartbeat every 30s             │
│    TimberbotPanel  (Launch / Stop widget)  │         │  agent run dispatcher                  │
└────────────────────────────────────────────┘         └────────────────────────────────────────┘
```

The HTTP listener handles request/response reads and writes. The WebSocket carries everything that used to be polled or pushed out-of-band: agent-state changes, the request-dispatch signal, and the 68-event game-event stream. Any number of WS clients can connect simultaneously — `tbot listen` is just another subscriber.

## Components

The mod has one read stack and one write stack:

- read: `TimberbotReadV2` — all GET endpoints, projection snapshots
- write: `TimberbotWrite` — all POST mutations
- entity lookup: `TimberbotEntityRegistry` — GUID/numeric ID bridge
- placement: `TimberbotPlacement` — building placement, A* path routing
- HTTP: `TimberbotHttpServer` — background listener, routing, ready-gate + auth middleware (port 8085)
- WebSocket: `TimberbotWebSocketServer` — parallel listener on `wsPort` (default 8086); accepts upgrades on `/api/ws`, broadcasts `state` and `event` frames, receives `heartbeat` / `ping` frames. Sibling event-bridge code lives in `TimberbotEvents.cs` (the `[OnEvent]` handlers there now publish into the WS broadcaster).
- debug: `TimberbotDebug` — reflection inspector, benchmark
- agent state: `TimberbotAgentState` — single source of truth for `mode`, `goal`, `ready`, `pendingRequest`, `lastAckedRequestId`, `lastError`. Persisted fields live in `state.json`; ephemeral fields reset on save load. Mutations raise a `Changed` event that the WS broadcaster turns into a `state` frame.
- UI: `TimberbotPanel` — movable in-game widget (Launch / Stop, mode dropdown, mode-aware textarea) + centered settings modal
- orchestrator: `TimberbotService` — lifecycle, settings, per-frame dispatch
- write jobs: `ITimberbotWriteJob` — budgeted write execution

## Thread model

```
MAIN THREAD (Unity)                         BACKGROUND THREADS
========================                    ==================
UpdateSingleton() [every frame]             HttpListener: ListenLoop() [blocking accept]
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
  |                                           WebSocket: per-connection async loops
  +-- AgentState.Changed (raised after mutate)  |
        |                                       +-- AcceptWebSocketAsync() upgrades on /api/ws
        +-- WS broadcaster builds frame         +-- receive loop: heartbeat / ping
              |                                 +-- send loop: state / event / pong / error
              +-- enqueue into per-conn         +-- slow consumer → dropped from queue
                  bounded send queues
```

| Location | Thread | Blocks game? |
|---|---|---|
| HTTP listener accept/GET response | background | no |
| GET endpoints | background | no |
| POST endpoints | main thread via `DrainRequests()` | yes, for duration |
| `ReadV2.ProcessPendingRefresh()` | main thread | yes, bounded by capture budget |
| `ProcessWriteJobs()` | main thread | yes, bounded by `writeBudgetMs` (default 1ms) |
| WS broadcast enqueue | main thread (after `Changed` / `[OnEvent]`) | negligible (drop-on-overflow) |
| WS send loop | background per connection | no |

## TimberbotService

`TimberbotService` is the singleton orchestrator.

It owns:

- settings load from `settings.json` and state load from `state.json`
- cached settings state and debounced writeback to `settings.json`
- HTTP and WebSocket server lifetimes (both refuse to start with non-localhost `listenAddress` and empty `authToken`)
- event bus registration
- `Registry.BuildAllIndexes()`
- `ReadV2.BuildAll()`
- the shared `TimberbotAgentState` instance exposed to the panel, the HTTP layer, and the WS broadcaster
- per-frame dispatch:
  - `DrainRequests()`
  - `ReadV2.ProcessPendingRefresh(now)`
  - `_server.ProcessWriteJobs(now, writeBudgetMs)`
  - `FlushSettingsIfNeeded(now)`

Settings behavior:

- runtime settings are loaded once in `Load()`
- the in-game settings UI mutates an in-memory `JObject`
- writes to disk are debounced (~1 second after the last change)
- `Unload()` forces a final flush

State behavior:

- `Load()` reads `state.json` and seeds `mode`, `goal`, `lastError`. Ephemeral fields (`ready`, `pendingRequest`, `lastAckedRequestId`) reset on every save load — the player must press Launch again after reloading a save.
- `Unload()` flushes `state.json` before shutting down the HTTP and WebSocket servers.
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
- `lastAckedRequestId` — for ack/clear bookkeeping; this is the same value the connector sends as `acked_request_id` in the WS heartbeat frame (snake_case on the wire, PascalCase in the C# field)

Every mutation raises a `Changed` event (outside the lock) that the WS broadcaster turns into a `state` frame. Subscribers therefore see state transitions push-style, with no polling.

### Ready gate

`TimberbotHttpServer` runs ready-gate middleware on every `/api/*` request. When `ready == false`, **both reads and writes** outside the carve-out return `409 {"error":"game_not_ready","hint":"player must press Launch in the Timberbot widget"}`.

Carve-out (always live):

- `/api/agent/*` — widget config + Launch trigger
- `/api/ready` — Launch / Stop toggle
- `/api/ping` — liveness probe

The WebSocket on port 8086 is **not** gated — the connector and any `tbot listen` subscriber can stay connected across save loads and Launch / Stop toggles. They just won't see useful state changes until the player presses Launch.

This is intentional. The gate is what makes the player the boss of the AI: nothing reads colony state, nothing places a building, nothing writes a save until the player explicitly opts in.

### Modes

- **Request mode** (default). Player types a prompt in the widget and presses Launch. The mod sets `pendingRequest`; the WS broadcaster pushes the resulting `state` frame to every connected subscriber. The connector picks up the request immediately — no polling.
- **Autonomous mode**. Connector decides cadence using the persisted `goal`. The ready gate is still authoritative — pressing Stop instantly flips `ready=false` on every connected client.

The connector advances `acked_request_id` in its WS `heartbeat` frame after each cycle so the mod can clear the single pending slot. Queueing is the connector's problem, not the mod's.

### Bearer-token auth

When `authToken` is set in `settings.json`, every `/api/*` route requires `Authorization: Bearer <token>` (constant-time compare). The same token is enforced on the WS upgrade request: either `Authorization: Bearer <token>` on the upgrade headers, or `?token=<token>` as a query-param fallback for browser clients that can't set upgrade headers. The mod **refuses to start** if `listenAddress` is non-localhost and `authToken` is empty — there's no path to ship the API over a LAN without a token. Tokens flow into the Python client via `[client].auth_token` in `config.toml`, the `TBOT_AUTH_TOKEN` env var, or `TimberbotClient(auth_token=...)`.

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
- a banner ("Connected to game session — waiting for player to Launch") appears when at least one WS subscriber is connected but the gate is off

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

## WebSocket protocol

A single long-lived WebSocket replaces the previous heartbeat-polling and outbound-HTTP-webhook channels. The mod hosts a parallel listener on `wsPort` (default `8086`); clients connect to `ws://host:wsPort/api/ws` and stay connected for the life of the session.

The authoritative wire contract — frame envelope, message types, auth header, reconnect guidance — lives in [`websocket-protocol.md`](websocket-protocol.md). Mod-side implementation notes:

- `TimberbotWebSocketServer` owns a separate `HttpListener` and async accept loop on `wsPort`. Upgrades go through `HttpListenerContext.AcceptWebSocketAsync()`.
- Each accepted connection gets a bounded send queue; slow consumers are dropped on overflow rather than back-pressuring the main thread.
- State broadcasts originate from `TimberbotAgentState.Changed` (raised outside the lock, after each mutation). Game-event broadcasts originate from the same `[OnEvent]` handlers in `TimberbotEvents.cs`; that file no longer makes HTTP calls — it just hands frames to the broadcaster.
- `TimberbotPure` ships pure helpers for the envelope: `BuildStateMessage`, `BuildEventMessage`, `ParseInboundMessage`.
- Auth: the same `authToken` middleware applies to upgrade requests. Bearer header preferred; `?token=` fallback for browser clients.
- Heartbeat cadence: 30 s. The client sends a `heartbeat` frame carrying `{version, agent_status, acked_request_id}`. WS ping/pong and TCP keepalive handle liveness — there is no shorter polling loop.
- Game events are **not** ready-gated. The ready gate only filters inbound `/api/*` HTTP requests; subscribers see weather / population / power events whether the player has pressed Launch or not.

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

The mod never spawns a process. The widget speaks `TimberbotHttpServer`; the connector speaks `TimberbotWebSocketServer`:

| Channel | Caller | Purpose |
|---|---|---|
| `GET /api/agent/state` (HTTP) | widget | One-shot read of `{mode, goal, ready, pendingRequest, agentStatus, lastError}` for UI bootstrap. The connector gets the same data as `state` frames on the WS. |
| `POST /api/agent/config` (HTTP) | widget | Debounced save of `mode` and/or `goal` |
| `POST /api/agent/request` (HTTP) | widget (Launch in request mode) | Set `pendingRequest`; triggers a `state` frame on every WS subscriber |
| `POST /api/ready` (HTTP) | widget | Launch / Stop the ready gate |
| `state` frame (WS, server→client) | connector, `tbot listen` | Full snapshot of agent state, pushed on every change |
| `event` frame (WS, server→client) | connector, `tbot listen` | Single game event with `{event, day, timestamp, data}` |
| `heartbeat` frame (WS, client→server) | connector | 30 s liveness ping carrying `{version, agent_status, acked_request_id}` |
| `ping` / `pong` frames (WS) | both sides | Optional application-level keepalive on top of the WS protocol-level ping/pong |

The connector (`tbot watch`) owns the agent process lifetime. It dispatches `tbot agent run` (or `opencode run --attach <url>`) on each `state` frame that surfaces a new `pendingRequest`, then sends a `heartbeat` frame with the new `acked_request_id` so the mod can clear the pending slot.

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

Registry data (GUID-to-ID maps, event-bridge lifecycle hooks). Updated on `EntityInitializedEvent`/`EntityDeletedEvent`. Used for entity lookup and compatibility only.

## Settings

`settings.json` in the mod folder:

```json
{
  "debugEndpointEnabled": true,
  "httpPort": 8085,
  "wsPort": 8086,
  "wsEnabled": true,
  "listenAddress": "127.0.0.1",
  "authToken": "",
  "writeBudgetMs": 1.0,
  "maxBodyBytes": 1048576,
  "widgetLeft": "123",
  "widgetTop": "456"
}
```

There are three categories of settings:

- runtime, read by `TimberbotService`: `debugEndpointEnabled`, `httpPort`, `wsPort`, `wsEnabled`, `writeBudgetMs`
- security, also applied at load:
  - `listenAddress` — bind address for both listeners; default `127.0.0.1`. Use `+`/`0.0.0.0` for LAN
  - `authToken` — bearer token required on every `/api/*` request and on every WS upgrade when set. **Required** if `listenAddress` is non-localhost; the mod refuses to start otherwise.
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

Deprecated `settings.json` keys are logged once at load and otherwise ignored:

- legacy agent-launcher: `terminal`, `pythonCommand`, `agentBinary`, `agentGoal`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`, `tbotCommand`. Backend choice, model, effort, and custom command templates live in `~/.config/timberbot/config.toml` (consumed by `tbot watch` and `tbot agent run`).
- legacy outbound webhooks: `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `webhookValidateUrls`. Events now flow over the WebSocket; there is no outbound HTTP delivery path.

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

The connector and event receiver (`tbot watch`, `tbot listen`) are unit-tested against an in-process aiohttp WebSocket server — they don't require a live game.

## Known debt

- `/api/debug` and benchmark surfaces are evolving
- capture budgeting is intentionally conservative and may need tuning per domain
- `BuildAlertsFromBuildings` and `BuildPowerFromBuildings` still allocate `.ToArray()` per call (1 array each)

## Related docs

- [`websocket-protocol.md`](websocket-protocol.md). authoritative WS wire contract (envelope, auth, reconnect)
- [`events.md`](events.md). user-facing guide to consuming the WS event stream
- [`fresh-on-request-snapshots.md`](fresh-on-request-snapshots.md). migration rationale and validation history
- [`thread-safe-surfaces.md`](thread-safe-surfaces.md). Timberborn thread-safety guidance
- [`developing.md`](developing.md). build, test, file structure
- [`performance.md`](performance.md). allocation audit, benchmarks, open issues
