# Timberbot

The mod is a pure server: HTTP for reads/writes on port 8085, plus a parallel WebSocket on port 8086 for state pushes and game events. Agents are driven by an out-of-process connector (`tbot watch`) gated by the in-game Launch button.

A C# mod + Python client that exposes a full read/write HTTP API for Timberborn plus a WebSocket event stream, enabling AI agents (Claude, ChatGPT, or custom scripts) to manage a beaver colony.

## Read First

Beyond this file:

- [`openapi.yaml`](openapi.yaml) — canonical HTTP contract
- [`docs/websocket-protocol.md`](docs/websocket-protocol.md) — canonical WS wire contract (envelope, auth, reconnect, message types)
- [`docs/api-reference.md`](docs/api-reference.md) — human-readable companion to the OpenAPI spec
- [`docs/architecture.md`](docs/architecture.md) — thread model, server split, write-job queue
- [`docs/devenv.md`](docs/devenv.md) — toolchain (.NET, Python, `ilspycmd`)

## Quick Reference

- **Build:** Open `timberbot/src/Timberbot.csproj` in an IDE with .NET support, or run `dotnet build` from that directory. The post-build target auto-deploys to the game's mod folder. Override the game DLL path with `-p:GameManagedDir=<path>` if the default doesn't match your install.
- **Run (game side):** Launch Timberborn with the mod enabled. The HTTP server starts on `httpPort` (default `8085`) and the WebSocket server on `wsPort` (default `8086`). Player presses **Launch** in the widget to open the ready gate.
- **Run (client side):** `tbot watch` is the long-running connector — it opens a single WebSocket to the mod. `tbot serve` is the Telegram bot mode — spawns an in-process MCP server (`127.0.0.1:8091` default) and routes agent output to Telegram (requires `TBOT_TELEGRAM_TOKEN` or `[serve.telegram].token` in `config.toml`; needs `pip install 'timberbot[serve]'`). `tbot listen` is a pure WS client for the game-event stream. `tbot <command>` and `tbot agent run` still work for one-shots. Install with `pipx install timberbot`.
- **Tests:** Python unit tests via `python -m pytest python/tests/`; C# xUnit tests via `dotnet test timberbot/test/`.

## Architecture

```
┌─ Timberborn (game process) ────────────────┐         ┌─ tbot watch (host process) ────────────┐
│  Timberbot.dll                             │         │                                        │
│    TimberbotHttpServer       :8085         │◀─HTTP──▶│  REST reads/writes (one-shots)         │
│    TimberbotWebSocketServer  :8086         │◀── WS ─▶│  long-lived ws://host:8086/api/ws      │
│    TimberbotAgentState  state.json         │         │  receives state + event frames         │
│    TimberbotReadV2 / TimberbotWrite        │         │  sends `heartbeat` every 30 s          │
│    TimberbotPanel       Launch / Stop      │         │  dispatches `tbot agent run` per cycle │
└────────────────────────────────────────────┘         └────────────────────────────────────────┘
```

The mod runs **inside** the Unity game process. It uses Timberborn's `Bindito` DI framework (not BepInEx or Harmony). All game DLLs are referenced with `Publicize="true"` to access internal APIs without reflection.

### Agent connector role

The mod no longer spawns the agent. Instead, the player runs `tbot watch` — a long-running Python process that:

1. Opens a single WebSocket to `ws://host:wsPort/api/ws` with exponential backoff until the game is reachable.
2. Receives `state` frames (full agent state on every change) and `event` frames (game events) push-style — no polling.
3. Sends a `heartbeat` frame every 30 s carrying `{version, agent_status, acked_request_id}`. WS ping/pong + TCP keepalive handle liveness.
4. Dispatches an `agent run` cycle when a new `pendingRequest` arrives via `state` frame, or when autonomous-mode cadence fires.
5. Sends the next `heartbeat` with an advanced `acked_request_id` so the mod clears the single `pendingRequest` slot.

`tbot watch` is the canonical place to add new orchestration logic (queueing, cadence, attach-to-`opencode serve`, multi-backend routing). The mod intentionally stays dumb.

### Ready gate

The widget's Launch / Stop button toggles `ready` on the mod. While `ready=false`, `TimberbotHttpServer` middleware returns `409 game_not_ready` for **every `/api/*` read and write** except the carve-out: `/api/agent/*`, `/api/ready`, `/api/ping`. The WebSocket on port 8086 is **not** ready-gated — clients stay connected across Launch / Stop toggles and continue to receive game-event frames (they just won't see anything useful happen on `state` frames until the player presses Launch).

`ready` is **not persisted**: it resets to `false` on every save load. The player has to opt in every session. `mode`, `goal`, and `lastError` persist via `state.json`; `pendingRequest` and `lastAckedRequestId` are in-memory only.

Bearer-token auth (`authToken` in `settings.json`) layers on top: when set, every `/api/*` request needs `Authorization: Bearer <token>` (constant-time compare), and every WS upgrade needs the same token (either `Authorization: Bearer <token>` on the upgrade headers or `?token=<token>` as a query-param fallback). The mod refuses to start if `listenAddress` is non-localhost and `authToken` is empty.

## Project Structure

```
timberbot/
├── docs/                        # Documentation (deployed with mod)
│   ├── api-reference.md         # Full API contract — read this for endpoints
│   ├── timberbot.md             # AI agent boot guide
│   ├── features.md              # Feature matrix
│   ├── getting-started.md       # Install & setup
│   └── architecture.md          # Internal design
├── design/                      # Design proposals + historical investigations
│   ├── automation-plan.md       # Plan for automation wiring extension
│   ├── automation-states.md     # Disambiguation reference for agents
│   └── …                        # Other design docs and implementation notes
├── agents/
│   └── beaver-developer.md      # Dev-agent prompt for working on this codebase
├── timberbot/
│   ├── src/                     # C# mod source
│   │   ├── Timberbot.csproj     # MSBuild project; manages game DLL refs & deploy
│   │   ├── TimberbotConfigurator.cs      # Bindito DI registration
│   │   ├── TimberbotHttpServer.cs        # HTTP listener, routing, ready-gate + auth middleware (port 8085)
│   │   ├── TimberbotWebSocketServer.cs   # WebSocket listener (port 8086): state + event broadcasts, heartbeat
│   │   ├── TimberbotReadV2.cs            # All GET endpoints (buildings, beavers, map)
│   │   ├── TimberbotWrite.cs             # All POST endpoints (pause, recipes, floodgates)
│   │   ├── TimberbotPlacement.cs         # Building/planting placement logic
│   │   ├── TimberbotAgentState.cs        # mode/goal/ready/pendingRequest container; state.json persistence; Changed event
│   │   ├── TimberbotEntityRegistry.cs    # Entity lookup by ID
│   │   ├── TimberbotEvents.cs           # [OnEvent] handlers that hand game events to the WS broadcaster
│   │   ├── TimberbotService.cs           # Main lifecycle (Load/Update); owns both listeners
│   │   ├── TimberbotPanel.cs             # In-game UI panel (Launch/Stop, mode dropdown)
│   │   ├── TimberbotDebug.cs             # Debug/diagnostic endpoints
│   │   ├── manifest.json                 # Mod metadata (name, version, min game version)
│   │   └── settings.json                 # Default config (port, listen address, authToken)
│   └── test/                    # C# xUnit tests (Tier 1+2 pure helpers)
├── python/
│   ├── pyproject.toml           # hatchling build; `tbot` console script
│   ├── src/timberbot/           # Python package source
│   │   ├── api/                 # TimberbotClient + Pydantic models
│   │   ├── cli/                 # `tbot` CLI commands and dispatcher
│   │   ├── agent/               # Pluggable backends + runner
│   │   ├── agent_prompts/       # Runtime prompts shipped as package data
│   │   ├── formatters/          # Map, dashboard, table renderers
│   │   ├── game_mcp/            # MCP server wrapping TimberbotClient as tools
│   │   ├── user_api/            # Telegram adapter + SessionManager for tbot serve
│   │   └── connector/           # WS connector shared by watch + serve
│   └── tests/                   # Unit + contract + integration suites
├── openapi.yaml                 # Single source of truth for the HTTP contract
├── mkdocs.yml                   # MkDocs config for documentation site
└── README.md
```

## Key Conventions

### C# Mod Side
- **DI framework:** Bindito, not Unity's built-in. Register services in `TimberbotConfigurator.cs` with `Bind<T>().AsSingleton()`.
- **Game DLL access:** Add references in `Timberbot.csproj` with `Publicize="true"` and `<Private>false</Private>`. Never ship game DLLs.
- **Thread safety:** HTTP requests arrive on a background thread. All game state mutations must be dispatched to the main thread via `ITimberbotWriteJob` queue pattern.
- **Entity lookup:** Use `TimberbotEntityRegistry` to find entities by integer ID. The registry is populated on game load.
- **State reading:** `TimberbotReadV2.cs` serializes game state to JSON. It uses `GetComponent<T>()` on entities to extract data from Timberborn's ECS-like component system.
- **State writing:** `TimberbotWrite.cs` processes mutations. Each write method finds the target entity, gets the relevant component, and calls the game's own setter methods.
- **Agent state:** `TimberbotAgentState` is the only source of truth for `mode`, `goal`, `ready`, `pendingRequest`, `lastAckedRequestId`, `lastError`. Reads/writes go through the container — don't add ad-hoc fields elsewhere. Mutations raise the `Changed` event (outside the lock) so the WS broadcaster can fan a `state` frame out to every subscriber.
- **Ready gate:** new `/api/*` endpoints must explicitly opt into the carve-out (`/api/agent/*`, `/api/ready`, `/api/ping`) or accept that they 409 when `ready=false`. Default is gated. The WebSocket on port 8086 is not ready-gated — events keep flowing.

### Python Client Side
- **Persistent state:** The agent uses `brain.toon` files in `memory/` subdirectories, keyed by settlement name. The `goal` parameter is saved here for cross-session persistence.
- **CLI pattern:** `tbot <command> key:value key:value`. Parameters are colon-separated, not `--flags`. Global flags (`--json`, `-v`/`--verbose`, `--debug`, `--host=`, `--port=`, `--auth-token=`) are recognised before the command and stripped before method dispatch. `-v` logs the resolved endpoint + each HTTP request to stderr; `-vv` / `--debug` also logs request/response bodies. `TBOT_DEBUG=1` env var forces DEBUG when an agent is shelling out to `tbot`.
- **Sequential mutations:** Always run mutating game API calls sequentially, never in parallel.
- **Boot flow:** Run the `brain` command once at session start to establish settlement context.

### Documentation
- `docs/timberbot.md` — primary AI-agent operating guide. Read this if you're touching the in-game agent behavior or the prompts.
- `docs/events.md` — user-facing guide for consuming the WS event stream.
- `python/src/timberbot/agent_prompts/timberbot.md` — system prompt shipped as `timberbot` package data and injected at runtime by `tbot agent run`.

`openapi.yaml`, `docs/websocket-protocol.md`, `docs/api-reference.md`, and `docs/architecture.md` are listed in [Read First](#read-first) above — they're load-bearing for any contract or threading change.

## Agent Tooling

### Developer-Agent Prompt

The repo ships [`agents/beaver-developer.md`](agents/beaver-developer.md) as a primary prompt for AI coding agents (Claude Code, Codex, etc.) working on this mod. It enforces the subagent-delegation pattern, the read-first list, and build/verify discipline.

### Understudy (optional in-game verification)

For behavioral changes where `dotnet build` and the Python smoke tests aren't enough, agents may install [Understudy](https://github.com/impuls42/understudy) — a Claude Code skill that runs Timberborn headless under `gamescope`/`sway`, injects synthetic input, and captures screenshots. Once installed per its README (`uv sync`, then `us stack install`), the agent can launch the game, exercise an endpoint, and verify the result without a human in the loop. Use it when the question is "did the game actually behave the way I expected?" — not as a replacement for unit tests.

## Game DLL Paths

The project references Timberborn game DLLs via the `$(GameManagedDir)` MSBuild property in `Timberbot.csproj`. All game DLLs use `Publicize="true"` and `<Private>false</Private>` — they are never shipped with the mod.

**Default paths (auto-detected by OS):**

| Platform | Default Path |
|----------|-------------|
| Linux | `~/.local/share/Steam/steamapps/common/Timberborn/Timberborn_Data/Managed/` |
| Windows | `C:\Games\Steam\steamapps\common\Timberborn\Timberborn_Data\Managed\` |

**Override at build time:**
```
dotnet build -p:GameManagedDir=/path/to/Timberborn/Timberborn_Data/Managed
```

**Key automation DLLs** (referenced with `Publicize="true"`):
- `Timberborn.Automation.dll` — Core wiring system (`Automator`, `Automatable`, `AutomatorConnection`)
- `Timberborn.AutomationBuildings.dll` — Sensor/relay/memory/timer/lever components (`Relay`, `Memory`, `DepthSensor`, etc.)

To inspect the API surface, decompile the DLLs locally with `ilspycmd` (see `docs/devenv.md`).

## Game Version Compatibility

- **Minimum game version:** 1.0.0.0 (set in `manifest.json`)
- **Automation system:** Introduced in Timberborn 1.0 (the full release, not an early access update). All automation buildings (sensors, relays, memory, timers, levers, gates) and the wiring system (`Automator`/`Automatable`/`AutomatorConnection`) are 1.0 features.
- **Modding framework:** The game uses `BaseComponent` (no longer inherits from MonoBehaviour as of 1.0). Components must implement interfaces like `IAwakableComponent` for lifecycle hooks.

## Current Limitations & Planned Work

The mod currently does **not** support:
- Reading automation wiring connections (which sensor is connected to which building input) — only the `inputName` of the connected transmitter is shown in building state
- Configuring automation components that lack a public setter (e.g., some sensor thresholds that are read-only at runtime)

See `design/automation-plan.md` for the full implementation plan with decompiled API surface from `Timberborn.Automation.dll` and `Timberborn.AutomationBuildings.dll`.

## External References

- Game automation guide: https://timberborn.org/articles/automation-guide
- Official modding tools: https://github.com/mechanistry/timberborn-modding/wiki
- Timberborn uses Unity Engine 6000.3.6f1 as of 1.0
