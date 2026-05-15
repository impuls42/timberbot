# Timberbot API

> **v0.9 — architecture rework, in flight.** The session-launch flow described here is the v0.9 shape (`tbot watch` connector + Launch button). Behavior on `master` may briefly lag while the rework lands.

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

You should see `{"status": "ok"}`. The API is only active while a game is loaded; it won't respond from the main menu. Note that `/api/ping` and `/api/agent/*` answer regardless of the ready gate, but every other `/api/*` endpoint returns `409 game_not_ready` until you press **Launch** in the in-game widget (see [Start a session](#start-a-session) below).

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

## Start a session

The widget no longer spawns the agent. Instead, the player runs `tbot watch` on their machine (the **agent connector**), and the widget's Launch button toggles the ready gate that lets the connector through.

The first-run flow is:

1. **Install the mod** (Steam Workshop or manual — see above).
2. **Install the CLI** via `pipx install timberbot` and run `tbot init` once.
3. **Configure a backend** in `~/.config/timberbot/config.toml` (`claude`, `codex`, `opencode`, or `custom`).
4. **Start the connector**: in a terminal, run `tbot watch`. Leave it running.
5. **Launch the game and load a save.** The Timberbot widget appears bottom-right.
6. **Press Launch.** The widget's state pill flips from `Not Ready` to `Idle`, and the connector dispatches the agent.

```bash
tbot watch                              # autonomous + request mode, no local listener
tbot watch --listen-port 9000           # also host a webhook listener for the fast path
tbot watch --attach-url http://127.0.0.1:4096   # talk to a long-running opencode serve
```

`tbot watch` reconnects with exponential backoff (1 s → 30 s cap), so you can start it before the game or restart the game without restarting the connector. While disconnected it logs every retry; once connected it heartbeats every 2 s and surfaces the current state in the terminal.

### Modes

The widget exposes two modes via a dropdown.

- **Request** *(default).* You type a prompt in the widget's textarea, press **Launch**, and the connector dispatches a single agent run for that prompt. Use this for "set up a plank chain", "place 3 farms near the river", or any discrete ask. Launching with an empty prompt is a no-op.
- **Autonomous.** The widget's textarea binds to a persistent `goal` (saved in `state.json`). Press Launch and the connector keeps dispatching agent runs at its configured cadence until you press **Stop**. Use this for "reach 50 beavers with 77 wellbeing" — the long-running objective.

Switching modes is instant and doesn't restart the connector. Stop is always one click away: it posts `{"ready": false}` to the mod, which **closes the gate** to every endpoint except `/api/agent/*`, `/api/ready`, `/api/tbot/*`, and `/api/ping`. The connector keeps heartbeating but won't drive any reads or writes until you Launch again.

### Per-backend defaults

The widget doesn't let you pick a model or effort — those live in `~/.config/timberbot/config.toml` (see [`config.toml`](#configtoml) below). The widget only owns mode + prompt/goal + Launch state.

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

When the mod has `authToken` set in `settings.json`, every `/api/*` route
except `/api/ping` requires an `Authorization: Bearer <token>` header:

```bash
curl -H "Authorization: Bearer $TBOT_AUTH_TOKEN" http://localhost:8085/api/summary
curl -X POST -H "Authorization: Bearer $TBOT_AUTH_TOKEN" \
     http://localhost:8085/api/speed -d '{"speed": 3}'
```

## Let AI play your colony

`tbot watch` is the normal entrypoint. It owns the agent process; the mod just owns the game state. The AI docs entrypoints are:

- the Timberbot agent prompt ships inside the `timberbot` Python package (`timberbot.agent_prompts.timberbot`); `tbot init` writes editable copies under your config dir
- [timberbot.md](timberbot.md) is the Timberbot Guide, the full operating guide behind that prompt
- [api-reference.md](api-reference.md) is the endpoint and response source of truth

### One-shot agent run

`tbot agent run` exists for one-shot dispatches without a long-running connector — handy for scripted tests or a single AI nudge:

```bash
tbot agent run --backend opencode --prompt "place 3 farms near the river"
tbot agent run --backend claude --goal "reach 50 beavers"      # autonomous-shaped prompt
tbot agent run --backend opencode --attach-url http://127.0.0.1:4096
```

`tbot agent run` builds the merged instructions file, talks to the running mod over HTTP to gather colony state, and spawns the agent CLI (or attaches to a long-running `opencode serve` via `--attach-url`). It does **not** open the ready gate — you still need to have pressed Launch in the widget, or `/api/*` reads will return `409 game_not_ready`.

### OpenAI Codex / other LLMs

Point Codex (or any other LLM with shell + HTTP) at the mod folder or repo root and at port 8085. After the player presses Launch, the agent has full read/write access. Paste `docs/timberbot.md` as the system prompt for non-Codex LLMs.

## Remote connections

By default the Python client connects to `127.0.0.1:8085`. Several ways to override:

```bash
tbot --host=192.168.1.50 --port=8085 summary       # per-invocation CLI flag
tbot --auth-token=s3cret summary                   # when the mod enforces auth
export TBOT_HOST=192.168.1.50 TBOT_PORT=8085       # per-shell env vars
export TBOT_AUTH_TOKEN=s3cret                      # bearer token (opt-in)
```

For a persistent default, drop a `config.toml` under your user config dir:

```toml
# ~/.config/timberbot/config.toml (Linux/macOS)
# %APPDATA%\timberbot\config.toml  (Windows)

[client]
host = "192.168.1.50"
port = 8085
auth_token = "..."          # required if the mod exposes a non-localhost listenAddress
```

For a multi-machine setup where the *mod itself* needs to accept non-localhost clients, flip `listenAddress` in the mod's `settings.json` to bind a reachable interface AND set `authToken` to a shared secret — the mod refuses to start with a non-localhost `listenAddress` and an empty `authToken`. Pass the same token to the client via `auth_token` in `config.toml`, the `TBOT_AUTH_TOKEN` env var, or `tbot --auth-token=…`. See [Settings and configuration](#settings-and-configuration-server-mod) for the full list.

## Configuration sources

Timberbot reads settings from three places, in this order (first match wins):

| Tier | Where | Owns |
|---|---|---|
| 1. CLI flags | `tbot --host=X --port=Y --auth-token=T --documents-dir=… --mod-dir=…` | per-invocation overrides |
| 2. Environment | `TBOT_HOST`, `TBOT_PORT`, `TBOT_AUTH_TOKEN`, `TBOT_DOCUMENTS_DIR`, `TBOT_MOD_DIR`, `TBOT_CONFIG_DIR` | per-shell overrides |
| 3. User config | `~/.config/timberbot/config.toml` (or platform equivalent) | per-user defaults — client target, bearer token, per-backend model/effort |
| 4. Mod settings | `Documents/Timberborn/Mods/Timberbot/settings.json` | mod runtime (port, security, webhook, `authToken`). `httpHost` here is a legacy client-side override. |
| 5. Built-in | hard-coded | `127.0.0.1:8085`, etc. |

### `config.toml`

The `tbot` CLI looks for a TOML file at your platform's user-config directory. Three sections matter today:

```toml
[client]
host = "127.0.0.1"        # default target host for the CLI
port = 8085               # default target port
auth_token = ""           # bearer token; required when the mod sets `authToken` (mandatory for non-localhost listenAddress)

[backends.claude]
model = "claude-opus-4-7"
effort = "high"

[backends.opencode]
model = "glm-4.6"
attach_url = "http://127.0.0.1:4096"   # attach to a long-running `opencode serve`

[backends.custom]
command = "aider --system-prompt-file {skill} {prompt}"   # template
```

Per-backend keys (`model`, `effort`, `command`, `binary`, `terminal_prefix`, `attach_url`) are fed into the `tbot agent run` argv — explicit CLI flags still win.

### Settings and configuration (server / mod)

The in-game `Settings` modal is the primary way to configure mod-side runtime.

All mod-side settings persist to `settings.json`:

- runtime: `debugEndpointEnabled`, `httpPort`, `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `writeBudgetMs`
- security: `listenAddress` (default `127.0.0.1`), `authToken` (required when `listenAddress` is non-localhost), `webhookValidateUrls` (default `true`), `maxBodyBytes` (default `1048576`)
- widget position: `widgetLeft`, `widgetTop`

Agent-shaped state lives in **`state.json`** alongside `settings.json`:

```json
{
  "mode": "request",
  "goal": "reach 50 beavers with 77 wellbeing",
  "lastError": null
}
```

The widget mutates `state.json` directly via `POST /api/agent/config`; you rarely edit it by hand. `ready`, `pendingRequest`, and the connector's registered webhook URL are in-memory only and reset on every save load.

Some runtime settings are applied on load, so changing them may require reloading the save or mod to fully apply.

!!! note "Deprecated settings keys"
    `terminal`, `pythonCommand`, `agentBinary`, `agentGoal`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`, and `tbotCommand` are no longer read by the mod. They are logged as ignored on load. Manage backend choice, per-backend model/effort/command defaults, and the path to the `tbot` console script via the user `config.toml` described above — the connector is the one that runs `tbot`, not the mod.

## macOS launch helper

`tbot launch settlement:<name>` still prepares `autoload.json` on macOS, but v1 does not auto-start Timberborn there. Run the command, then open Timberborn manually and the mod will auto-load the selected save from the main menu.

## Troubleshooting

!!! warning "Connection refused / no response on port 8085"
    - The API only runs while a game is loaded. It won't respond from the main menu or loading screen.
    - Check that the mod is enabled in the Mod Manager.
    - Windows Firewall may block the port. The mod binds the address from `listenAddress` (default `127.0.0.1`); set it to `+`/`0.0.0.0` only with an `authToken` in place.

!!! warning "`409 game_not_ready` on every endpoint"
    The player has not pressed Launch yet. The ready gate refuses **all `/api/*` reads and writes** except `/api/agent/*`, `/api/ready`, `/api/tbot/*`, and `/api/ping` while `ready=false`. Open the in-game widget and press Launch.

!!! warning "`401 unauthorized`"
    The mod has `authToken` set in `settings.json` but the client isn't sending `Authorization: Bearer <token>`. Set `auth_token` in `~/.config/timberbot/config.toml`, export `TBOT_AUTH_TOKEN`, or pass `tbot --auth-token=…`.

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
