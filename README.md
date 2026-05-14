# Timberbot API

<p align="center">
  <img src="timberbot/src/thumbnail.png" alt="Timberbot — a cybernetic beaver playing Timberborn at a desk">
</p>

**Status: active. mod works, still adding features**

> Modified fork of [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). Extends the original mod with an expanded read/write HTTP API, automation wiring endpoints, webhooks, and AI-agent integrations. All credit for the original mod goes to [abix-](https://github.com/abix-).

[Getting Started](docs/getting-started.md) | [API Reference](docs/api-reference.md) | [Upstream](https://github.com/abix-/TimberbornMods)

C# mod + Python client that lets AI agents read and control a running Timberborn game over HTTP.

```
Timberborn (Unity)
  |-- Timberbot API mod (port 8085)   read and write game state

Python client
  |-- timberbot.py                   single-file API client + CLI + dashboard
```

## Quick start

```bash
# with Timberborn running + mod loaded
timberbot.py summary                              # colony snapshot
timberbot.py buildings                            # list all buildings
timberbot.py beavers                              # beaver wellbeing + critical needs
timberbot.py map x1:110 y1:130 x2:130 y2:150     # ASCII map with terrain + blockers
timberbot.py place_building prefab:Path x:100 y:130 z:2 orientation:south
timberbot.py place_path x1:110 y1:130 x2:130 y2:150       # A* pathfinding with auto-stairs
timberbot.py set_speed speed:3                    # fast forward
timberbot.py science                              # science points + unlockable buildings
timberbot.py distribution                         # import/export settings per district
timberbot.py link source_id:42 target_id:44 input:a       # wire sensor -> building
timberbot.py configure_automation id:42 property:threshold value:50
timberbot.py top                                  # live colony dashboard
timberbot.py                                      # list all methods
```

Auto-launch a save directly:

```bash
timberbot.py launch settlement:MyCastle save:day5
```

Or use raw HTTP. no Python needed:

```bash
curl http://localhost:8085/api/summary
curl -X POST http://localhost:8085/api/speed -d '{"speed": 3}'
```

## Features

- **A* pathfinding**. `place_path` routes around obstacles, water, and ruins with auto-stairs
- **Automation wiring**. link/unlink sensors, relays, and memory cells to any pausable building, and configure thresholds, modes, and logic over HTTP
- **Fresh-on-request reads**. no stale data, zero cost when idle
- **Blocker tracking**. ruins and editor objects visible in /api/tiles and placement errors
- **Write job system**. budgeted frame execution, no spikes
- **Debug endpoint**. reflection inspector with chaining and validation
- **Webhooks**. subscribe to game events over HTTP
- **Safe by default**. binds to localhost, validates webhook URLs, caps request body size, and gates agent launches behind an allowlist
- **Zero-alloc hot path**. no garbage collection pressure on read endpoints

## Docs

- [Getting Started](docs/getting-started.md). install, first steps, examples
- [API Reference](docs/api-reference.md). all HTTP endpoints
- [Timberbot AI](docs/timberbot.md). AI guide for agents playing Timberborn
- [Architecture](docs/architecture.md). internals, thread model, read/write pipeline
- [Automation Plan](docs/automation-plan.md). decompiled wiring API and `/api/automation/*` design
- [Agent Prompts](agents/). drop-in prompts for `timberbot`, `scout`, `wirer`, `auditor`, and `beaver-developer` workflows
- [Repo Guide](AGENTS.md). project layout, build commands, conventions
- [Developing](docs/developing.md). build from source, add endpoints, Workshop publishing

## Settings

Drop a `settings.json` in your mod folder (`Documents/Timberborn/Mods/Timberbot/`):

```json
{
  "httpPort": 8085,
  "listenAddress": "localhost",
  "debugEndpointEnabled": true,
  "webhooksEnabled": true,
  "webhookBatchMs": 200,
  "webhookCircuitBreaker": 30,
  "writeBudgetMs": 1.0,
  "agentAllowlistEnabled": true,
  "webhookValidateUrls": true,
  "maxBodyBytes": 1048576
}
```

All fields are optional. missing keys use defaults. The server binds to `localhost` by default. set `listenAddress` to `+` or `0.0.0.0` to accept LAN connections.

## Requirements

- Timberborn (Steam)
- .NET SDK 6+ (to build the mod)
- Python 3.8+ with `requests` (for the client, optional)
- `pip install toons` for compact TOON output (optional, falls back to JSON)

## Credits

Forked from:

- [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). upstream Timberbot mod. this repo is a modified fork with added HTTP API, automation wiring, webhooks, and AI-agent tooling

Learned from these Timberborn modding projects:

- [mechanistry/timberborn-modding](https://github.com/mechanistry/timberborn-modding). official modding tools, wiki, and examples
- [thomaswp/BeaverBuddies](https://github.com/thomaswp/BeaverBuddies). `BlockObjectPlacerService.Place()` for building placement, `TemplateInstantiator` + `MarkAsPreviewAndInitialize` + `IsValid()` for game-native placement validation, `BuildingUnlockingService.Unlock()` for science, `WorkingHoursManager` for work schedules
- [datvm/TimberbornMods](https://github.com/datvm/TimberbornMods). `TreeCuttingArea.AddCoordinates()` for tree marking, `IAlertFragment` patterns for building alerts
- [ihsoft/TimberbornMods](https://github.com/ihsoft/TimberbornMods). `Inventories.AllInventories` for building inventory, `BuildingUnlockingService.Unlocked()` for science checks
- [CordialGnom/timberborn-unity-modding](https://github.com/CordialGnom/timberborn-unity-modding). `PlantingService.SetPlantingCoordinates()` for crop planting, `PlantingAreaValidator.CanPlant()` for planting validation
- [Timberborn-KyP-Mods/TimberPrint](https://github.com/Timberborn-KyP-Mods/TimberPrint). `PreviewFactory` + `BlockValidator` patterns for placement validation
- [toon-format/toon](https://github.com/toon-format/toon). Token-Oriented Object Notation for compact AI output
