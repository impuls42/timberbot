# Timberbot API

<p align="center">
  <img src="timberbot/src/thumbnail.png" alt="Timberbot — a cybernetic beaver playing Timberborn at a desk">
</p>

**Status: gameplay is stable; AI integration and automation features are under active development**

> Modified fork of [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). Extends the original mod with an expanded read/write HTTP API, automation wiring endpoints, a WebSocket event/state channel, and AI-agent integrations. All credit for the original mod goes to [abix-](https://github.com/abix-).

[Getting Started](docs/getting-started.md) | [API Reference](docs/api-reference.md) | [WebSocket Protocol](docs/websocket-protocol.md) | [Upstream](https://github.com/abix-/TimberbornMods)

C# mod + Python client that lets AI agents read and control a running Timberborn game over HTTP, with a parallel WebSocket channel for push-style state and events.

```
Timberborn (Unity)
  |-- Timberbot API mod
        |-- HTTP server  on :8085  read + write game state
        |-- WS server    on :8086  /api/ws — state pushes + game events

Python (PyPI: `timberbot`)
  |-- `tbot` CLI                     dashboard, agent launcher, raw API client
  |-- `tbot watch`                   long-running WS connector that drives an agent
  |-- `tbot listen`                  WS event subscriber (CLI + scripts)
  |-- `from timberbot import …`      typed Pydantic client for scripts
```

## Quick start

```bash
pipx install timberbot      # or: pip install timberbot
tbot init                   # materialize agent prompts into ~/.config/timberbot
```

```bash
# with Timberborn running + mod loaded
tbot summary                                       # colony snapshot
tbot buildings                                     # list all buildings
tbot beavers                                       # beaver wellbeing + critical needs
tbot map --x1=110 --y1=130 --x2=130 --y2=150               # ASCII map with terrain + blockers
tbot place_building --prefab=Path --x=100 --y=130 --z=2 --orientation=south
tbot place_path --x1=110 --y1=130 --x2=130 --y2=150        # A* pathfinding with auto-stairs
tbot set_speed 3                                   # fast forward (positional)
tbot science                                       # science points + unlockable buildings
tbot distribution                                  # import/export settings per district
tbot link --source-id=42 --target-id=44 --input=a  # wire sensor -> building
tbot configure_automation --id=42 --property=threshold --value=50
tbot brain                                         # live colony state + persistent memory
tbot top                                           # live colony dashboard
tbot                                               # list all commands
```

Auto-launch a save (Linux/Windows) or generate Steam launch args (macOS):

```bash
tbot launch --settlement=MyCastle --save=day5
```

`tbot` itself is a pure network client and does not read the game's Documents
tree. On Linux/Steam Deck the build-time `scripts/deploy.sh` autodiscovers the
Proton/Wine `Documents/Timberborn/Mods/Timberbot/` folder when copying the
freshly built DLL; set `TBOT_DOCUMENTS_DIR` for non-`steamuser` Wine prefixes.

Or use raw HTTP. no Python needed:

```bash
curl http://127.0.0.1:8085/api/summary
curl -X POST http://127.0.0.1:8085/api/speed -d '{"speed": 3}'
```

Live state + game events over the WebSocket:

```bash
tbot listen --pretty                                # subscribe to ws://127.0.0.1:8086/api/ws
tbot watch --backend claude                         # long-running agent connector
```

## Features

- **WebSocket push channel**. State changes and game events stream over `ws://<host>:8086/api/ws` as `{type, payload}` JSON frames. See [WebSocket Protocol](docs/websocket-protocol.md).
- **Ready gate**. Player presses **Launch** in the in-game widget to authorize agent activity; until then all `/api/*` calls (read + write) return `409 game_not_ready` except `/api/agent/*`, `/api/ready`, `/api/ping`.
- **`tbot watch` connector**. Long-running Python process that connects over WS, heartbeats, and dispatches agent runs (request or autonomous mode).
- **A* pathfinding**. `place_path` routes around obstacles, water, and ruins with auto-stairs.
- **Automation wiring**. Link/unlink sensors, relays, memory cells to any pausable building, and configure thresholds, modes, logic over HTTP.
- **Fresh-on-request reads**. No stale data, zero cost when idle.
- **Blocker tracking**. Ruins and editor objects visible in `/api/tiles` and placement errors.
- **Write job system**. Budgeted frame execution, no spikes.
- **Debug endpoint**. Reflection inspector with chaining and validation.
- **Bearer auth**. Optional `authToken` setting; mandatory when `listenAddress` is non-loopback.
- **Safe by default**. Binds to `127.0.0.1`, refuses to start with a non-loopback listenAddress unless `authToken` is set, caps request body size.
- **Zero-alloc hot path**. No garbage collection pressure on read endpoints.

## Docs

- [Getting Started](docs/getting-started.md). install, first steps, examples
- [API Reference](docs/api-reference.md). all HTTP endpoints
- [WebSocket Protocol](docs/websocket-protocol.md). `/api/ws` frame envelope, message types, reconnect
- [Events](docs/events.md). consume the game-event stream with `tbot listen` or a custom WS client
- [Timberbot Guide](docs/timberbot.md). AI guide for agents playing Timberborn
- [Architecture](docs/architecture.md). internals, thread model, read/write pipeline, WS broadcaster
- [Automation Plan](design/automation-plan.md). decompiled wiring API and `/api/automation/*` design
- [Agent Prompts](python/src/timberbot/agent_prompts/). drop-in gameplay prompts (`timberbot`, `scout`, `wirer`, `auditor`, `connector-mode`). Materialize editable copies into your user config dir with `tbot init`. The development-agent prompt for working on this codebase lives separately at [`agents/beaver-developer.md`](agents/beaver-developer.md).
- [Repo Guide](AGENTS.md). project layout, build commands, conventions
- [Developing](docs/developing.md). build from source, add endpoints, cutting a GitHub release

## Settings

Drop a `settings.json` in your mod folder (`Documents/Timberborn/Mods/Timberbot/`):

```json
{
  "httpPort": 8085,
  "wsPort": 8086,
  "wsEnabled": true,
  "listenAddress": "127.0.0.1",
  "authToken": "",
  "debugEndpointEnabled": false,
  "maxBodyBytes": 1048576
}
```

All fields are optional. Missing keys use defaults. The server binds to
`127.0.0.1` by default; set `listenAddress` to `+` or `0.0.0.0` to accept LAN
connections — but then `authToken` becomes mandatory (the mod refuses to start
otherwise).

Older settings keys are ignored with a one-line deprecation warning at load:

- Agent-spawn keys (`terminal`, `pythonCommand`, `agentBinary`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`) — manage agent-side defaults via `~/.config/timberbot/config.toml` instead.
- Webhook keys (`webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `webhookValidateUrls`) — outbound HTTP webhooks are gone; subscribe to the WS event stream instead (`tbot listen` or a custom WS client).

## Requirements

- Timberborn (Steam)
- .NET SDK 6+ (to build the mod)
- Python 3.10+ (for the `tbot` CLI; install via `pipx install timberbot`)

## Credits

Forked from:

- [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). upstream Timberbot mod. this repo is a modified fork with added HTTP API, automation wiring, WebSocket event stream, and AI-agent tooling

Learned from these Timberborn modding projects:

- [mechanistry/timberborn-modding](https://github.com/mechanistry/timberborn-modding). official modding tools, wiki, and examples
- [thomaswp/BeaverBuddies](https://github.com/thomaswp/BeaverBuddies). `BlockObjectPlacerService.Place()` for building placement, `TemplateInstantiator` + `MarkAsPreviewAndInitialize` + `IsValid()` for game-native placement validation, `BuildingUnlockingService.Unlock()` for science, `WorkingHoursManager` for work schedules
- [datvm/TimberbornMods](https://github.com/datvm/TimberbornMods). `TreeCuttingArea.AddCoordinates()` for tree marking, `IAlertFragment` patterns for building alerts
- [ihsoft/TimberbornMods](https://github.com/ihsoft/TimberbornMods). `Inventories.AllInventories` for building inventory, `BuildingUnlockingService.Unlocked()` for science checks
- [CordialGnom/timberborn-unity-modding](https://github.com/CordialGnom/timberborn-unity-modding). `PlantingService.SetPlantingCoordinates()` for crop planting, `PlantingAreaValidator.CanPlant()` for planting validation
- [Timberborn-KyP-Mods/TimberPrint](https://github.com/Timberborn-KyP-Mods/TimberPrint). `PreviewFactory` + `BlockValidator` patterns for placement validation
- [toon-format/toon](https://github.com/toon-format/toon). Token-Oriented Object Notation for compact AI output
