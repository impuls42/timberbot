# Timberbot

A C# mod + Python client that exposes a full read/write HTTP API for Timberborn, enabling AI agents (Claude, ChatGPT, or custom scripts) to manage a beaver colony.

## Quick Reference

- **Build:** Open `timberbot/src/Timberbot.csproj` in an IDE with .NET support, or run `dotnet build` from that directory. The post-build target auto-deploys to the game's mod folder. Override the game DLL path with `-p:GameManagedDir=<path>` if the default doesn't match your install.
- **Run (game side):** Launch Timberborn with the mod enabled. The HTTP server starts on the port configured in `settings.json` (default `8085`).
- **Run (client side):** `python timberbot/script/timberbot.py <command> [params]` or use the `tbot` alias if on PATH.
- **Venv:** `venv/` at repo root, activate with `source venv/bin/activate`. Contains `requests` and `toons`.
- **Tests:** `timberbot/test/` — run via `python -m pytest` from the `timberbot/test` directory.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Timberborn Game Process                            │
│  ┌───────────────────────────────────────────────┐  │
│  │ Timberbot.dll (C# mod, loaded via Bindito DI) │  │
│  │  ├─ TimberbotHttpServer   (HTTP listener)     │  │
│  │  ├─ TimberbotReadV2       (GET /api/*)        │  │
│  │  ├─ TimberbotWrite        (POST /api/*)       │  │
│  │  ├─ TimberbotPlacement    (building placement)│  │
│  │  ├─ TimberbotAgent        (AI agent runner)   │  │
│  │  └─ TimberbotWebhook      (outbound webhooks) │  │
│  └───────────────────────────────────────────────┘  │
│              ▲ HTTP :8085                           │
└──────────────┼──────────────────────────────────────┘
               │
┌──────────────┼──────────────────────────────────────┐
│  Python Client (timberbot.py)                       │
│  ├─ CLI commands (brain, buildings, place, etc.)    │
│  └─ Persistent state via brain.toon in memory/      │
└─────────────────────────────────────────────────────┘
```

The mod runs **inside** the Unity game process. It uses Timberborn's `Bindito` DI framework (not BepInEx or Harmony). All game DLLs are referenced with `Publicize="true"` to access internal APIs without reflection.

## Project Structure

```
timberbot/
├── docs/                        # Documentation (deployed with mod)
│   ├── api-reference.md         # Full API contract — read this for endpoints
│   ├── timberbot.md             # AI agent boot guide
│   ├── features.md              # Feature matrix
│   ├── getting-started.md       # Install & setup
│   ├── architecture.md          # Internal design
│   └── automation-plan.md       # Plan for automation wiring extension
├── agents/
│   └── timberbot.md             # Runtime prompt injected into AI agents at launch
├── timberbot/
│   ├── src/                     # C# mod source
│   │   ├── Timberbot.csproj     # MSBuild project; manages game DLL refs & deploy
│   │   ├── TimberbotConfigurator.cs      # Bindito DI registration
│   │   ├── TimberbotHttpServer.cs        # HTTP listener, routing
│   │   ├── TimberbotReadV2.cs            # All GET endpoints (buildings, beavers, map)
│   │   ├── TimberbotWrite.cs             # All POST endpoints (pause, recipes, floodgates)
│   │   ├── TimberbotPlacement.cs         # Building/planting placement logic
│   │   ├── TimberbotAgent.cs             # AI agent process management
│   │   ├── TimberbotEntityRegistry.cs    # Entity lookup by ID
│   │   ├── TimberbotWebhook.cs           # Outbound webhook dispatch
│   │   ├── TimberbotService.cs           # Main lifecycle (Load/Update)
│   │   ├── TimberbotPanel.cs             # In-game UI panel
│   │   ├── TimberbotDebug.cs             # Debug/diagnostic endpoints
│   │   ├── manifest.json                 # Mod metadata (name, version, min game version)
│   │   └── settings.json                 # Default config (port, listen address)
│   ├── script/
│   │   └── timberbot.py          # Python CLI client (1800+ lines)
│   └── test/                     # Test scripts
├── mkdocs.yml                    # MkDocs config for documentation site
├── venv/                         # Python virtual environment
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

### Python Client Side
- **Persistent state:** The agent uses `brain.toon` files in `memory/` subdirectories, keyed by settlement name. The `goal` parameter is saved here for cross-session persistence.
- **CLI pattern:** `timberbot.py <command> key:value key:value`. Parameters are colon-separated, not `--flags`.
- **Sequential mutations:** Always run mutating game API calls sequentially, never in parallel.
- **Boot flow:** Run the `brain` command once at session start to establish settlement context.

### Documentation
- `docs/timberbot.md` is the primary AI agent guide — always read first.
- `docs/api-reference.md` is the definitive API contract — never improvise endpoint details.
- `python/src/tbot/agent_prompts/timberbot.md` is the system prompt shipped as `tbot` package data and injected at runtime by `tbot agent run`.

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

See `docs/automation-plan.md` for the full implementation plan with decompiled API surface from `Timberborn.Automation.dll` and `Timberborn.AutomationBuildings.dll`.

## Known Issues

- **macOS agent execution:** `TimberbotAgent.cs` has a bug in the command execution path that forces Windows-specific logic when a terminal is provided, and fails when it isn't. The platform detection around line 680 needs fixing.

## External References

- Game automation guide: https://timberborn.org/articles/automation-guide
- Official modding tools: https://github.com/mechanistry/timberborn-modding/wiki
- Timberborn uses Unity Engine 6000.3.6f1 as of 1.0
