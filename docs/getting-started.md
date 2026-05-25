# Timberbot API

**Full read/write HTTP API for controlling Timberborn with AI, plus a WebSocket channel for live state and game events.**

Timberbot API gives Claude, Codex, ChatGPT, or your own scripts complete access to your beaver colony over HTTP. read game state, place buildings, manage workers, plant crops, and keep your beavers alive.

!!! info "Modified fork"
    This project is a modified fork of [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). It extends the original mod with an expanded read/write HTTP API, automation wiring endpoints, a WebSocket event stream, and AI-agent integrations. All credit for the original mod goes to [abix-](https://github.com/abix-).

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

This fork is **not** published to the Steam Workshop — the Workshop entry "Timberbot API" is the upstream [`abix-/TimberbornMods`](https://github.com/abix-/TimberbornMods) project. Install this fork manually from GitHub releases (or build from source — see [Developing](developing.md)).

Download `Timberbot.dll`, `manifest.json`, and `thumbnail.png` from the [latest GitHub release](https://github.com/impuls42/timberbot/releases) and place them in:

```
C:\Users\<you>\Documents\Timberborn\Mods\Timberbot\           # Windows
~/Documents/Timberborn/Mods/Timberbot/                        # macOS, native Linux
~/.steam/steam/steamapps/compatdata/1062090/pfx/drive_c/users/steamuser/Documents/Timberborn/Mods/Timberbot/   # Linux + Proton
```

Enable the mod in the Mod Manager. If you previously had the upstream Workshop version installed, disable or unsubscribe it first — both registering the same singleton would prevent the mod from loading.

## Verify it works

Start a game (or load a save). Open a browser to:

```
http://127.0.0.1:8085/api/ping
```

You should see `{"status": "ok", "ready": true, "openapiVersion": "..."}`. The API is only active while a game is loaded; it won't respond from the main menu. Note that `/api/ping` and `/api/agent/*` answer regardless of the ready gate, but every other `/api/*` endpoint returns `409 game_not_ready` until you press **Launch** in the in-game widget (see [Start a session](#start-a-session) below).

!!! note "127.0.0.1 vs localhost"
    The mod's `listenAddress` defaults to `127.0.0.1` (PR #10). Under Mono's `HttpListener` the prefix matches the `Host:` header exactly — a server bound to `127.0.0.1` rejects `Host: localhost` with HTTP 400. Either use `http://127.0.0.1:...` everywhere or change `listenAddress` to `localhost` in `settings.json`.

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

`tbot` is a pure network client — it does not read or write anything under the game's `Documents/Timberborn/` tree. As long as the mod is reachable at `127.0.0.1:8085` (or wherever `--host=`/`TBOT_HOST` points), the CLI works without any local Timberborn install. Per-settlement `brain.toon` files live under the OS user-data dir (`~/.local/share/timberbot/memory/<settlement>/`) instead.

The build-time `scripts/deploy.sh` still autodiscovers the Proton/Wine `Documents/Timberborn/Mods/Timberbot/` folder so source builds can deploy the freshly built DLL — set `TBOT_DOCUMENTS_DIR` if your Wine prefix uses a non-`steamuser` username.

## Start a session

The widget no longer spawns the agent. Instead, the player runs `tbot watch` on their machine (the **agent connector**), and the widget's Launch button toggles the ready gate that lets the connector through.

The first-run flow is:

1. **Install the mod** from GitHub releases (see above).
2. **Install the CLI** via `pipx install timberbot` and run `tbot init` once.
3. **Configure a backend** in `~/.config/timberbot/config.toml` (`claude`, `codex`, `opencode`, or `custom`).
4. **Start the connector**: in a terminal, run `tbot watch`. Leave it running.
5. **Launch the game and load a save.** The Timberbot widget appears bottom-right.
6. **Press Launch.** The widget's state pill flips from `Not Ready` to `Idle`, and the connector dispatches the agent.

```bash
tbot watch                                       # autonomous + request mode
```

`tbot watch` opens a single long-lived WebSocket to the mod (`ws://host:wsPort/api/ws`, default port 8086) and stays connected for the life of the session. It reconnects with exponential backoff (1 s → 30 s cap), so you can start it before the game or restart the game without restarting the connector. While disconnected it logs every retry; once connected it receives `state` and `event` frames push-style and sends a 30 s `heartbeat`. There is no `--listen-port` — the connector has no inbound HTTP server.

If you just want to watch the event stream (drought warnings, building events, …) without driving an agent, use the standalone WS client `tbot listen`. See [Events](events.md) for details.

### Modes

The widget exposes two modes via a dropdown.

- **Request** *(default).* You type a prompt in the widget's textarea, press **Launch**, and the connector dispatches a single agent run for that prompt. Use this for "set up a plank chain", "place 3 farms near the river", or any discrete ask. Launching with an empty prompt is a no-op.
- **Autonomous.** The widget's textarea binds to a persistent `goal` (saved in `state.json`). Press Launch and the connector keeps dispatching agent runs at its configured cadence until you press **Stop**. Use this for "reach 50 beavers with 77 wellbeing" — the long-running objective.

Switching modes is instant and doesn't restart the connector. Stop is always one click away: it posts `{"ready": false}` to the mod, which **closes the gate** to every endpoint except `/api/agent/*`, `/api/ready`, and `/api/ping`. The connector keeps the WebSocket open but won't drive any reads or writes until you Launch again.

### Per-backend defaults

The widget doesn't let you pick a model or effort — those live in `~/.config/timberbot/config.toml` (see [`config.toml`](#configtoml) below). The widget only owns mode + prompt/goal + Launch state.

## Talk to the agent over Telegram (`tbot serve`)

`tbot watch` dispatches one-shot agent runs triggered by the in-game widget. `tbot serve` is the *interactive* alternative: it runs the agent as a long-lived ACP session and routes user input through a Telegram bot, while exposing the game as an MCP server the agent observes in real time.

```bash
pip install 'timberbot[serve]'              # pulls fastmcp + python-telegram-bot
npm i -g @agentclientprotocol/claude-agent-acp   # ACP bridge for the claude backend
export TBOT_TELEGRAM_TOKEN=123456:AA…       # from @BotFather
tbot serve                                   # foreground; Ctrl-C to stop
```

The claude backend no longer drives `claude` directly — Claude Code 2.1.x removed
the `--acp` flag. Instead `tbot serve` spawns Zed's standalone ACP bridge
`claude-agent-acp`, which speaks the Agent Client Protocol and drives the Claude
Agent SDK underneath. Install it globally with `npm i -g
@agentclientprotocol/claude-agent-acp` (or rely on `npx -y
@agentclientprotocol/claude-agent-acp`, at the cost of a cold-start download on
the first run). The bridge inherits Anthropic auth from the Claude Agent SDK
(keychain/OAuth or `ANTHROPIC_API_KEY`); set that up the same way you would for
`claude`. The `opencode` backend is unchanged — it still exposes `opencode acp`.

What it starts (one process, three concurrent tasks via `asyncio.TaskGroup`):

1. **Game MCP server** — `fastmcp` HTTP/SSE on `127.0.0.1:8091` by default. Wraps `TimberbotClient` as 60 tools. Every tool response carries an *event envelope* (`meta.cursor`, `meta.events`, `meta.advisory`, `meta.hint`) so the agent sees game-side changes (droughts, building collapses, beaver deaths) without polling. The connector spawns the ACP agent (`claude-agent-acp` for the claude backend, `opencode acp` for opencode) and points it at this URL via an `sse`-transport MCP server entry.
2. **ACP connector** — speaks standard Agent Client Protocol (JSON-RPC 2.0, `protocolVersion: 1`) over the agent subprocess's stdin/stdout. Tool permission requests are auto-resolved against `[serve] allowed_tools` (glob patterns, default `["game.*"]`) — the user is **never** prompted to approve MCP tool calls. The bridge titles MCP tools `mcp__<server>__<tool>`, which the connector normalizes to `<server>.<tool>` so `game.*` keeps matching the game tools.
3. **Telegram adapter** — long-polls Telegram's Bot API for `/prompt`, `/cancel`, `/halt`, `/status` commands. Streaming agent output edits a single Telegram message (500-char or 500ms flush window) to stay under Telegram's edit-rate limits. Game elicitation choices render as inline-keyboard buttons.

### First-run Telegram setup

1. Open Telegram, search for `@BotFather`, send `/newbot`. Pick a name and username. BotFather returns a token like `7912345678:AAH9w7xY…`.
2. Set the token in **one** of these (precedence: CLI > env > config):
    - `tbot serve --telegram-token 7912345678:AAH…`
    - `export TBOT_TELEGRAM_TOKEN=7912345678:AAH…`
    - `~/.config/timberbot/config.toml`:
      ```toml
      [serve.telegram]
      token = "7912345678:AAH..."
      ```
3. Start a chat with your bot (find it by the username you chose, send `/start`).
4. Run `tbot serve`. From Telegram, send `/prompt build a plank chain near the river`.

### What you can do from Telegram

The bot accepts these inbound surfaces:

| Telegram input | What happens |
|---|---|
| `/prompt <text>` | First time: spawns the agent subprocess, starts an ACP session pointed at the game MCP server, and forwards `<text>` as the initial prompt. Subsequent times: forwarded as a follow-up turn on the same session. |
| `/cancel` | Sends `session/cancel` to the agent. The connector transitions to the `HALTING` state and acks with a status message. |
| `/halt` | Same as `/cancel` for now — both are wired to the ACP cancel path. |
| `/status` | Replies with the current ACP session state (`active`, `halting`, `ended`, or `no session`). |
| *Tap an inline-keyboard button* | Sent when the game has asked the agent a `game/elicitation` choice — your tap is forwarded back to the agent as `User selected: <choice>` on its next turn. |

What you'll see come back:

| Source | Telegram rendering |
|---|---|
| Agent replies and reasoning (`session/update` `agent_message_chunk` / `agent_thought_chunk`, streaming) | A single Telegram message that gets edited in place as the agent talks — flush throttled to 500 chars or 500 ms so Telegram doesn't rate-limit. |
| Game elicitation (`game/elicitation`) — the game has asked a player-only question | A new message with an inline keyboard, one button per choice. |
| Session lifecycle (`active`, `halting`, `ended`) | A short plain-text status line. |

Tool permission requests for MCP tools never reach Telegram — they are auto-approved against `[serve] allowed_tools` (default `["game.*"]`) or auto-rejected, so the player only sees game-relevant prompts.

!!! note "Game elicitation is inactive under `claude-agent-acp`"
    `game/elicitation` was a Timberbot-specific notification the old `claude --acp` emitted by forwarding MCP server-initiated elicitations. Standard ACP has no elicitation primitive and the bridge consumes those events internally, so the inline-keyboard prompts above **don't currently fire** with the claude backend. The connector keeps the handler in place; restoring the surface is tracked as a follow-up.

### `tbot serve` flags

```text
tbot serve [--backend {claude,opencode}] [--model MODEL] [--acp-binary PATH]
           [--telegram-token TOKEN] [--mcp-host HOST] [--mcp-port N]
           [--ws-port N] [--no-wait] [--verbose]
```

| Flag | Env | Config | Default | What it does |
|---|---|---|---|---|
| `--backend` | — | `[serve] backend` | `claude` | Which ACP runtime to spawn. Only `claude` or `opencode` are accepted. |
| `--model` | — | `[serve] model` | `claude-opus-4-7` | Model identifier passed to the agent CLI. Set this when switching backends (e.g. `glm-4.6` for opencode). |
| `--acp-binary` | — | `[serve] acp_binary` | `claude-agent-acp` (claude) / `opencode` (opencode) | Path or name of the ACP agent CLI. Use this if the bridge or `opencode` isn't on `$PATH` (e.g. `npx -y @agentclientprotocol/claude-agent-acp`). |
| `--telegram-token` | `TBOT_TELEGRAM_TOKEN` | `[serve.telegram] token` | — *(required)* | Bot token from BotFather. `tbot serve` exits with an error if missing. |
| `--mcp-host` | — | `[serve] mcp_host` | `127.0.0.1` | Bind address for the game MCP HTTP/SSE server. |
| `--mcp-port` | — | `[serve] mcp_port` | `8091` | Port for the game MCP HTTP/SSE server. |
| `--ws-port` | `TBOT_WS_PORT` | `[client] ws_port` | `8086` | Mod-side WebSocket port (shared with `tbot watch` / `tbot listen`). |
| `--no-wait` | — | — | off *(wait by default)* | Fail fast if the mod isn't reachable at startup. By default `tbot serve` retries the startup ping with exp_backoff (1 s → 30 s cap) until the mod responds, so you can launch the server first and start the game afterwards. Pass `--no-wait` in scripts/CI where you'd rather see a clean exit if the mod is down. Matches the existing UX of `tbot watch` and `tbot listen`, which already reconnect on their own. |
| `--verbose` / `-v` | — | — | WARNING | Logging level. `-v` = INFO, `-vv` = DEBUG. |
| — | — | `[serve] allowed_tools` | `["game.*"]` | Glob list of MCP tool names the connector auto-approves. Anything else is auto-rejected without prompting the user. |
| — | — | `[serve.telegram] allowed_users` | `[]` *(open + warn)* | List of Telegram user IDs allowed to talk to the bot. Empty/unset means **any** Telegram user who finds the bot can `/prompt` it — `tbot serve` logs a warning at startup in that case. |

`--host` / `--port` / `--auth-token` from the global `tbot` flags still apply: they configure the connection from `tbot serve` *to the running mod*, the same as for `tbot summary`.

### Example `[serve]` config

```toml
# ~/.config/timberbot/config.toml
[serve]
backend = "claude"
model = "claude-opus-4-7"
mcp_host = "127.0.0.1"
mcp_port = 8091
allowed_tools = ["game.*"]   # only game tools auto-approved; everything else rejected

[serve.telegram]
token = "7912345678:AAH..."
allowed_users = [12345678]    # your Telegram user ID (see Troubleshooting); empty/unset = open bot + WARN
```

For opencode the model needs to be set explicitly since the default targets Claude:

```toml
[serve]
backend = "opencode"
model = "glm-4.6"
```

### How it differs from `tbot watch`

| | `tbot watch` | `tbot serve` |
|---|---|---|
| Trigger | In-game widget Launch button + autonomous cadence | Telegram `/prompt` command |
| Agent lifetime | One process per dispatch, then exit | One long-lived ACP session per user |
| Events to agent | Logged but not pushed to the agent | Embedded in every MCP tool response (`meta.events`) |
| User input | Set once via widget textarea | Continuous via Telegram chat |
| Required infrastructure | None (just the mod) | MCP server + Telegram bot |

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
tbot set_speed 3                            # fast forward (0=pause, 1/2/3)
tbot map --x1=110 --y1=130 --x2=130 --y2=150          # ASCII map with terrain height shading
tbot place_path --x1=120 --y1=140 --x2=120 --y2=150   # route a path with auto-stairs
```

!!! note "CLI flag syntax"
    `tbot` is dispatched via [python-fire](https://github.com/google/python-fire). Pass arguments either positionally (`tbot set_speed 3`) or as flags (`tbot set_speed --speed=3`). Hyphens and underscores are interchangeable: `--source-id=42` and `--source_id=42` both work. Run `tbot <command> --help` to see the positional/flag layout for any command.

!!! note "Pagination"
    List endpoints (buildings, beavers, trees, crops) return 100 items by default. Use `--limit=0` for all items, or `--limit=N --offset=M` for pages. Filter server-side with `--name=X` or `--x=N --y=N --radius=R`.

### Visual map

`map` renders a colored ASCII grid of your colony. Background shading shows terrain height, characters represent buildings, trees, water, and crops. A legend is printed below the grid.

```bash
tbot map --x1=110 --y1=130 --x2=130 --y2=150
```

### Live dashboard

```bash
tbot top
```

Live colony dashboard. Population, resources, weather, drought countdown, wellbeing breakdown, alerts. all updating in real time.

### Write commands

Commands that change game state take typed positional/flag arguments (run any command with `--help` to see its signature):

```bash
tbot place_building --prefab=Path --x=120 --y=130 --z=2 --orientation=south
tbot set_priority --id=12340 --priority=VeryHigh
tbot plant_crop --x1=110 --y1=130 --x2=115 --y2=135 --z=2 --crop=Carrot
tbot mark_trees --x1=100 --y1=120 --x2=110 --y2=130 --z=2
```

Get building IDs from `tbot buildings`. Get prefab names from `tbot prefabs`.

### Automation wiring

Wire sensors to relay/buildings to pause them based on thresholds:

```bash
tbot buildings --name=Relay                                       # find relay IDs
tbot link --source-id=42 --target-id=44 --input=a                 # wire sensor → relay input
tbot configure_automation --id=42 --property=threshold --value=50
tbot unlink --id=44 --input=a                                     # remove a wire
```

See [API Reference](api-reference.md#post-apiautomationlink) for the full wiring API.

### Colony memory

`brain` combines a live colony snapshot with persistent goal/task/location state stored in `~/.local/share/timberbot/memory/<settlement>/`:

```bash
tbot brain                                                  # live summary + saved goals and tasks
tbot brain --goal="reach 50 beavers with 77 wellbeing"     # set a persistent goal
```

### Raw HTTP

You don't need Python for raw HTTP calls alone. But Python is required for the
normal Timberbot workflow, including `tbot` commands and the in-game agent
launcher (which shells out to `tbot agent run`).

```bash
curl http://127.0.0.1:8085/api/summary
curl http://127.0.0.1:8085/api/buildings
curl -X POST http://127.0.0.1:8085/api/speed -d '{"speed": 3}'
curl -X POST http://127.0.0.1:8085/api/building/place -d '{"prefab": "Path", "x": 120, "y": 130, "z": 2, "orientation": 0}'
```

When the mod has `authToken` set in `settings.json`, every `/api/*` route
except `/api/ping` requires an `Authorization: Bearer <token>` header:

```bash
curl -H "Authorization: Bearer $TBOT_AUTH_TOKEN" http://127.0.0.1:8085/api/summary
curl -X POST -H "Authorization: Bearer $TBOT_AUTH_TOKEN" \
     http://127.0.0.1:8085/api/speed -d '{"speed": 3}'
```

## Let AI play your colony

`tbot watch` is the normal entrypoint. It owns the agent process; the mod just owns the game state. The AI docs entrypoints are:

- the Timberbot agent prompt ships inside the `timberbot` Python package (`timberbot.agent_prompts.timberbot`); `tbot init` writes editable copies under your config dir
- [timberbot.md](timberbot.md) is the Timberbot Guide, the full operating guide behind that prompt
- [api-reference.md](api-reference.md) is the endpoint and response source of truth

### One-shot agent run

`tbot agent run` exists for one-shot dispatches without a long-running connector — handy for scripted tests or a single AI nudge:

```bash
tbot agent run --backend opencode --goal "place 3 farms near the river"
tbot agent run --backend claude --goal "reach 50 beavers"              # autonomous-shaped prompt
tbot agent run --backend opencode --goal "..." --attach-url http://127.0.0.1:4096
```

`--goal` is the user instruction. `--prompt NAME` (a separate, optional flag) selects which packaged system prompt to merge in; the default is `timberbot`.

`tbot agent run` builds the merged instructions file, talks to the running mod over HTTP to gather colony state, and spawns the agent CLI (or attaches to a long-running `opencode serve` via `--attach-url`). It does **not** open the ready gate — you still need to have pressed Launch in the widget, or `/api/*` reads will return `409 game_not_ready`.

### OpenAI Codex / other LLMs

Point Codex (or any other LLM with shell + HTTP) at the mod folder or repo root and at port 8085. After the player presses Launch, the agent has full read/write access. Paste `docs/timberbot.md` as the system prompt for non-Codex LLMs.

## Verbose and debug output

When you can't tell *what* the client is doing or *where* it's pointed, turn on logging:

```bash
tbot -v summary                                    # INFO: resolved endpoint + each HTTP request
tbot -vv summary                                   # DEBUG: also request/response bodies
tbot --debug summary                               # alias for -vv
TBOT_DEBUG=1 tbot summary                          # env-driven DEBUG (useful when an agent shells out to tbot)
```

A typical `-v` run prints lines like:

```
12:04:18 timberbot.cli INFO dispatch method=summary -> http://127.0.0.1:8085 (host=default port=default auth=none)
12:04:18 timberbot.api.client INFO -> GET /api/summary
12:04:18 timberbot.api.client INFO <- GET /api/summary 200 (3142 B in 41 ms)
```

The `(host=… port=… auth=…)` tag tells you *where each setting came from* — `cli` / `env` / `config` / `default` / `none`. Use it to debug "why is my client hitting the wrong server" or "why is no auth being sent". Common failure modes (`409 game_not_ready`, `401 unauthorized`, connection refused) print a one-line actionable hint to stderr at the default WARNING level — you don't need `-v` to see them.

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

Timberbot reads client settings from four places, in this order (first match wins):

| Tier | Where | Owns |
|---|---|---|
| 1. CLI flags | `tbot --host=X --port=Y --auth-token=T` | per-invocation overrides |
| 2. Environment | `TBOT_HOST`, `TBOT_PORT`, `TBOT_AUTH_TOKEN`, `TBOT_WS_PORT`, `TBOT_TELEGRAM_TOKEN`, `TBOT_CONFIG_DIR`, `TBOT_DATA_DIR` | per-shell overrides |
| 3. User config | `~/.config/timberbot/config.toml` (or platform equivalent) | per-user defaults — client target, bearer token, per-backend model/effort, `tbot serve` settings |
| 4. Built-in | hard-coded | `127.0.0.1:8085`, etc. |

The mod's `settings.json` is the canonical place for **server-side** settings (`httpPort`, `wsPort`, `listenAddress`, `authToken`, etc.) and is not consulted for client endpoint resolution — use `config.toml` or the env vars above for client overrides.

### `config.toml`

The `tbot` CLI looks for a TOML file at your platform's user-config directory. The sections that matter:

```toml
[client]
host = "127.0.0.1"        # default target host for the CLI
port = 8085               # default target port (mod HTTP API)
ws_port = 8086            # default target port (mod WebSocket)
auth_token = ""           # bearer token; required when the mod sets `authToken` (mandatory for non-localhost listenAddress)

[backends.claude]         # used by `tbot watch` and `tbot agent run`
model = "claude-opus-4-7"
effort = "high"

[backends.opencode]
model = "glm-4.6"
attach_url = "http://127.0.0.1:4096"   # attach to a long-running `opencode serve`

[backends.custom]
command = "aider --system-prompt-file {skill} {prompt}"   # template

[serve]                   # used by `tbot serve` only
backend = "claude"        # or "opencode"
model = "claude-opus-4-7"
acp_binary = "claude-agent-acp"   # ACP agent CLI; default: claude-agent-acp (claude) / opencode (opencode)
mcp_host = "127.0.0.1"
mcp_port = 8091
allowed_tools = ["game.*"]

[serve.telegram]
token = "7912345678:AAH..."  # also: TBOT_TELEGRAM_TOKEN env, --telegram-token flag
allowed_users = [12345678]   # Telegram user IDs allowed to talk to the bot; empty = open + WARN
```

Per-backend keys under `[backends.*]` (`model`, `effort`, `command`, `binary`, `terminal_prefix`, `attach_url`) are fed into the `tbot agent run` argv — explicit CLI flags still win. The `[serve]` section is read only by `tbot serve` — it intentionally does *not* fall back to `[backends.*]` so the two stacks can have independent model/binary choices.

### Settings and configuration (server / mod)

The in-game `Settings` modal is the primary way to configure mod-side runtime.

All mod-side settings persist to `settings.json`:

- runtime: `debugEndpointEnabled`, `httpPort` (default `8085`), `wsPort` (default `8086`), `wsEnabled` (default `true`), `writeBudgetMs`
- security: `listenAddress` (default `127.0.0.1` — applies to both listeners), `authToken` (required when `listenAddress` is non-localhost; enforced on every `/api/*` request and on every WS upgrade), `maxBodyBytes` (default `1048576`)
- widget position: `widgetLeft`, `widgetTop`

Agent-shaped state lives in **`state.json`** alongside `settings.json`:

```json
{
  "mode": "request",
  "goal": "reach 50 beavers with 77 wellbeing",
  "lastError": null
}
```

The widget mutates `state.json` directly via `POST /api/agent/config`; you rarely edit it by hand. `ready` and `pendingRequest` are in-memory only and reset on every save load.

Some runtime settings are applied on load, so changing them may require reloading the save or mod to fully apply.

!!! note "Deprecated settings keys"
    `terminal`, `pythonCommand`, `agentBinary`, `agentGoal`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`, and `tbotCommand` are no longer read by the mod. They are logged as ignored on load. Manage backend choice, per-backend model/effort/command defaults, and the path to the `tbot` console script via the user `config.toml` described above — the connector is the one that runs `tbot`, not the mod.

## macOS launch helper

`tbot launch --settlement=<name>` on macOS prints the Steam launch options for the chosen settlement (`--tb-settlement <name> [--tb-save <save>]`) instead of starting the game. Paste those into Steam → Timberborn → Properties → Launch Options once, then open Timberborn manually — the mod's `TimberbotAutoLoad` reads the `--tb-*` args at the main menu and loads the save.

## Troubleshooting

!!! warning "Connection refused / no response on port 8085"
    - The API only runs while a game is loaded. It won't respond from the main menu or loading screen.
    - Check that the mod is enabled in the Mod Manager.
    - Windows Firewall may block the port. The mod binds the address from `listenAddress` (default `127.0.0.1`); set it to `+`/`0.0.0.0` only with an `authToken` in place.

!!! warning "`409 game_not_ready` on every endpoint"
    The player has not pressed Launch yet. The ready gate refuses **all `/api/*` reads and writes** except `/api/agent/*`, `/api/ready`, and `/api/ping` while `ready=false`. Open the in-game widget and press Launch.

!!! warning "`401 unauthorized`"
    The mod has `authToken` set in `settings.json` but the client isn't sending `Authorization: Bearer <token>`. Set `auth_token` in `~/.config/timberbot/config.toml`, export `TBOT_AUTH_TOKEN`, or pass `tbot --auth-token=…`.

!!! warning "No module named 'requests' / 'toons'"
    `pipx install timberbot` pulls these in automatically. If you installed via
    `pip` into the system Python and dependencies are missing, reinstall via
    `pipx` so the CLI gets its own environment.

!!! warning "`tbot serve` errors: 'requires extra dependencies'"
    `tbot serve` needs `fastmcp` and `python-telegram-bot`, which ship in the optional `[serve]` extra. Install with `pip install 'timberbot[serve]'` (or `pipx install 'timberbot[serve]'`).

!!! warning "`tbot serve` errors: 'no Telegram token found'"
    Set `TBOT_TELEGRAM_TOKEN`, pass `--telegram-token`, or add `[serve.telegram] token = "..."` to `config.toml`. The token comes from `@BotFather` on Telegram.

!!! warning "`tbot serve` agent never starts: 'claude-agent-acp: command not found' in logs"
    The connector spawns the ACP agent via `acp_binary` (default: `claude-agent-acp` for the claude backend, `opencode` for opencode). Install the bridge with `npm i -g @agentclientprotocol/claude-agent-acp`, or point `--acp-binary` at an explicit launcher such as `npx -y @agentclientprotocol/claude-agent-acp` or `/full/path/to/binary` (also settable as `[serve] acp_binary = "…"`).

!!! warning "`tbot serve` agent stderr: `error: unknown option '--acp'`"
    Your `--acp-binary` is still pointing at `claude`. Claude Code 2.1.x removed the `--acp` flag; the claude backend now uses the standalone bridge. Install it (`npm i -g @agentclientprotocol/claude-agent-acp`) and either drop the `[serve] acp_binary` override or set it to `claude-agent-acp`, then re-run.

!!! warning "`tbot serve` logs 'no allowed_users configured' at startup"
    The Telegram bot is currently open to anyone who guesses its username. Find your numeric Telegram user ID (DM `@userinfobot`, or look at any of your messages via the Telegram API), then add it to `~/.config/timberbot/config.toml`:
    ```toml
    [serve.telegram]
    allowed_users = [12345678]
    ```
    Any user not in this list is silently dropped — they get no acknowledgement that the bot exists. Empty/unset means "open + warn" (current behaviour, kept for backwards compatibility).

!!! bug "Building placement creates ghost buildings"
    Failed placements can sometimes create invisible entities. See [Known Issues](api-reference.md#known-issues) in the API reference.

---

- [API Reference](api-reference.md). every endpoint with request/response examples
- [Timberbot Guide](timberbot.md). full operating guide for gameplay and AI behavior
- [Features](features.md). what's implemented vs gaps
- [Developing](developing.md). build from source
