# Timberbot API

**Full read/write HTTP API for controlling Timberborn with AI.**

Timberbot API gives Claude, Codex, ChatGPT, or your own scripts complete access to your beaver colony over HTTP. read game state, place buildings, manage workers, plant crops, and keep your beavers alive.

!!! info "Modified fork"
    This project is a modified fork of [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). It extends the original mod with an expanded read/write HTTP API, automation wiring endpoints, webhooks, and AI-agent integrations. All credit for the original mod goes to [abix-](https://github.com/abix-).

---

## What you can do

| | Read | Write |
|---|---|---|
| **Buildings** | All buildings with workers, power, priority, inventory | Place, demolish, pause, configure |
| **Beavers** | Wellbeing, needs, workplace, contamination | Migrate between districts (in-progress) |
| **Resources** | Per-district stocks, distribution settings | Set import/export, stockpile config |
| **Map** | Terrain, water, occupants, contamination | Plant crops, mark trees, route paths |
| **Colony** | Weather, science, alerts, notifications | Speed, work hours, unlock buildings |

---

## Install the mod

### From Steam Workshop

Subscribe to Timberbot API on the Steam Workshop. The mod installs automatically. Launch Timberborn and enable it in the Mod Manager.

### Manual install

Download `Timberbot.dll`, `manifest.json`, and `thumbnail.png` from the [latest GitHub release](https://github.com/impuls42/timberbot/releases) and place them in:

```
C:\Users\<you>\Documents\Timberborn\Mods\Timberbot\
```

On macOS, use:

```
~/Documents/Timberborn/Mods/Timberbot/
```

Enable the mod in the Mod Manager.

## Verify it works

Start a game (or load a save). Open a browser to:

```
http://localhost:8085/api/ping
```

You should see `{"status": "ok", "ready": true}`. The API is only active while a game is loaded. it won't respond from the main menu.

## Install the Timberbot CLI

The Python CLI ships as the `timberbot` PyPI package. Install with `pipx` so it
gets its own virtual environment:

```bash
pipx install timberbot         # or: pip install timberbot
tbot init                      # materialize editable agent prompts under your config dir
```

This creates the `tbot` console command. The Python import is `timberbot`:

```python
from timberbot import TimberbotClient
```

### Linux / Steam Deck

`tbot` autodiscovers Timberborn's "Documents" folder, including Proton/Wine
prefixes under `~/.steam/steam/steamapps/compatdata/<appid>/pfx/...`. The
scan assumes the standard Proton-managed Windows username `steamuser` — if
you're running Timberborn under a custom Wine prefix with a different
username, set `TBOT_DOCUMENTS_DIR` explicitly. To force a specific
location:

```bash
export TBOT_DOCUMENTS_DIR=~/.steam/steam/steamapps/compatdata/1062090/pfx/drive_c/users/steamuser/Documents/Timberborn
# or per-invocation:
tbot --documents-dir=/path/to/Timberborn summary
tbot --mod-dir=/path/to/Mods/Timberbot summary
```

## Preferred AI workflow: in-game Timberbot UI

The preferred way to use Timberbot with Claude, Codex, or opencode is the
in-game Timberbot widget.

1. Start a game or load a save.
2. Look for the green `Timberbot API` widget in the bottom-right corner.
3. Click `Settings`.
4. Pick a **Backend** (claude / codex / opencode / custom) and enter a **Goal**.
5. Click `Start`.

The mod shells out to `tbot agent run --backend <name> --goal "<goal>"`. The
Python CLI loads the merged instructions file, talks to the running mod over
HTTP to gather colony state, and spawns the selected agent CLI. Per-backend
defaults (model, effort, custom command template) live in
`~/.config/timberbot/config.toml` — the in-game panel only chooses goal +
backend.

## Output formats

=== "TOON (default)"

    Compact tabular format designed for AI consumption and quick scanning:

    ```bash
    tbot summary
    ```

=== "JSON"

    Full nested data for programmatic access:

    ```bash
    tbot --json summary
    ```

The same applies to the HTTP API: add `?format=json` to GET requests, or `"format": "json"` in POST bodies. Without it, endpoints that support both formats default to TOON.

## First API commands

```bash
tbot                                        # list all commands with usage
tbot summary                                # colony snapshot: population, resources, weather, alerts
tbot buildings                              # all buildings with workers, priority, power
tbot beavers                                # wellbeing and critical needs per beaver
tbot set_speed speed:3                      # fast forward (0=pause, 1/2/3)
tbot map x1:110 y1:130 x2:130 y2:150              # ASCII map with terrain height shading
tbot place_path x1:120 y1:140 x2:120 y2:150  # route a path with auto-stairs
```

!!! note "Pagination"
    List endpoints (buildings, beavers, trees, crops) return 100 items by default. Use `limit:0` for all items, or `limit:N offset:M` for pages. Filter server-side with `name:X` or `x:N y:N radius:R`.

### Visual map

`map` renders a colored ASCII grid of your colony. Background shading shows terrain height, characters represent buildings, trees, water, and crops. A legend is printed below the grid.

```bash
tbot map x1:110 y1:130 x2:130 y2:150
```

### Live dashboard

```bash
tbot top
```

Live colony dashboard. Population, resources, weather, drought countdown, wellbeing breakdown, alerts. all updating in real time.

### Write commands

Commands that change game state use `key:value` arguments:

```bash
tbot place_building prefab:Path x:120 y:130 z:2 orientation:south
tbot set_priority id:12340 priority:VeryHigh
tbot plant_crop x1:110 y1:130 x2:115 y2:135 z:2 crop:Carrot
tbot mark_trees x1:100 y1:120 x2:110 y2:130 z:2
```

Get building IDs from `tbot buildings`. Get prefab names from `tbot prefabs`.

### Raw HTTP

You don't need Python for raw HTTP calls alone. But Python is required for the
normal Timberbot workflow, including `tbot` commands and the in-game agent
launcher (which shells out to `tbot agent run`).

```bash
curl http://localhost:8085/api/summary
curl http://localhost:8085/api/buildings
curl -X POST http://localhost:8085/api/speed -d '{"speed": 3}'
curl -X POST http://localhost:8085/api/building/place -d '{"prefab": "Path", "x": 120, "y": 130, "z": 2, "orientation": 0}'
```

## Let AI play your colony

The mod also ships docs for AI play with Claude Code, OpenAI Codex, ChatGPT, or any AI agent that can make HTTP calls. This is optional if you prefer the in-game UI workflow.

The AI docs entrypoints are:

- the Timberbot agent prompt ships inside the `timberbot` Python package (`timberbot.agent_prompts.timberbot`); `tbot init` writes editable copies under your config dir
- [timberbot.md](timberbot.md) is the Timberbot Guide, the full operating guide behind that prompt
- [api-reference.md](api-reference.md) is the endpoint and response source of truth

### Launch via `tbot agent run`

```bash
pipx install timberbot                               # console script: tbot
tbot init                                            # materialize prompts into your user config dir
tbot agent run --backend opencode --goal "reach 50 beavers"
```

`tbot agent run` builds the merged instructions file, talks to the running mod over HTTP to gather colony state, and spawns the agent CLI (`claude`, `codex`, `opencode`, or a custom template). Run `tbot agent list-backends` for the full list and `tbot agent prompts` to see installed prompts.

### OpenAI Codex

Point Codex at the mod folder (or repo root). It can call the HTTP API directly on port 8085. The docs in `docs/` give it everything it needs.

### Other LLMs

Paste the contents of `docs/timberbot.md` as the system prompt. Keep `docs/api-reference.md` available for exact command and error details. The Steam Workshop install ships the same docs under `Documents/Timberborn/Mods/Timberbot/docs`, and the GitHub repo mirrors the same content if users need another copy.

## Remote connections

By default the Python client connects to `127.0.0.1:8085`. To connect to a game running on another machine:

```bash
tbot --host=192.168.1.50 --port=8085 summary
```

Or set defaults in `settings.json` (mod folder):

```json
{
  "httpHost": "192.168.1.50",
  "httpPort": 8085
}
```

The client reads `httpHost` and `httpPort` from settings.json when no CLI flags are given. CLI flags take precedence. See [architecture.md](architecture.md#settings) for all settings.

## Settings and configuration

The in-game `Settings` modal is the primary way to configure Timberbot.

All settings persist to `settings.json`, including:

- agent UI settings: `agentBinary` (backend choice) and `agentGoal`; plus widget position
- runtime settings: `debugEndpointEnabled`, `httpPort`, `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `writeBudgetMs`
- security settings: `listenAddress` (default `localhost`), `webhookValidateUrls` (default `true`), `maxBodyBytes` (default `1048576`)

Editing `settings.json` directly is the advanced/manual path. The normal path
is to change settings in-game and let Timberbot save them for you.

Some runtime settings are applied on load, so changing them may require
reloading the save or mod to fully apply.

!!! note "Deprecated settings keys"
    `terminal`, `pythonCommand`, `agentBinary` (as a path), `agentModel`,
    `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, and
    `agentAllowedBinaries` are no longer read by the mod. They are logged as
    ignored on load. Manage per-backend model/effort/command defaults via
    `~/.config/timberbot/config.toml` (Linux/macOS) or
    `%APPDATA%/timberbot/config.toml` (Windows).

## macOS launch helper

`tbot launch settlement:<name>` still prepares `autoload.json` on macOS, but v1 does not auto-start Timberborn there. Run the command, then open Timberborn manually and the mod will auto-load the selected save from the main menu.

## Troubleshooting

!!! warning "Connection refused / no response on port 8085"
    - The API only runs while a game is loaded. It won't respond from the main menu or loading screen.
    - Check that the mod is enabled in the Mod Manager.
    - Windows Firewall may block the port. The mod tries `http://+:8085/` first (all interfaces), then falls back to `http://localhost:8085/` if that fails.

!!! warning "No module named 'requests' / 'toons'"
    `pipx install timberbot` pulls these in automatically. If you installed via
    `pip` into the system Python and dependencies are missing, reinstall via
    `pipx` so the CLI gets its own environment.

!!! bug "Building placement creates ghost buildings"
    Failed placements can sometimes create invisible entities. See [Known Issues](api-reference.md#known-issues) in the API reference.

---

- [API Reference](api-reference.md). every endpoint with request/response examples
- [Timberbot Guide](timberbot.md). full operating guide for gameplay and AI behavior
- [Features](features.md). what's implemented vs gaps
- [Developing](developing.md). build from source
