# Configuration Reference

This page is the file-by-file reference for everything Timberbot reads and writes on disk. For the step-by-step setup walkthrough, see [Getting Started](getting-started.md); this page is the lookup table behind it.

Timberbot has two configuration *sides*:

- the **mod** (the C# server running inside Timberborn) owns server-side runtime and security settings — its files live next to the mod under `Documents/Timberborn/Mods/Timberbot/`.
- the **client** (the `tbot` CLI and the `timberbot` Python package) owns the connection target, per-backend defaults, and `tbot serve` settings — its files live under your OS user-config and user-data directories.

The mod's `settings.json` is **not** consulted for client endpoint resolution, and the client never reads `settings.json`. The two sides communicate only over HTTP/WebSocket.

## Files at a glance

| File | Owner | Format | Lives in | You edit it? |
|---|---|---|---|---|
| [`config.toml`](#configtoml-client) | client (`tbot`) | TOML | [config dir](#client-config-directory) | Yes — primary client config |
| [`settings.json`](#settingsjson-mod) | mod (C# server) | JSON | [mod dir](#mod-directory) | Yes — but the in-game Settings modal is the primary surface |
| [`state.json`](#statejson-mod) | mod (C# server) | JSON | [mod dir](#mod-directory) | Rarely — the widget writes it via `POST /api/agent/config` |
| [`brain.toon`](#braintoon-per-settlement-memory) | client (`tbot`) | TOON | [data dir](#client-data-directory) | Rarely — managed by `tbot brain` and location/task commands |
| [`agent_prompts/*.md`](#agent-prompts-tbot-init) | client (`tbot`) | Markdown | [config dir](#client-config-directory) | Yes — editable copies materialized by `tbot init` |

`config.toml` and `settings.json` are the two you configure by hand. `state.json` and `brain.toon` are machine-managed state — documented here so you know what they are, not because you normally edit them.

## File locations

All three roots follow OS conventions and each has an environment-variable override.

### Client config directory

Holds `config.toml` and the materialized `agent_prompts/`. Resolved by `timberbot.config.config_dir()`:

| Platform | Location |
|---|---|
| Linux | `$XDG_CONFIG_HOME/timberbot`, falling back to `~/.config/timberbot` |
| macOS | `~/Library/Application Support/timberbot` |
| Windows | `%APPDATA%\timberbot`, falling back to `~/AppData/Roaming/timberbot` |

Override the whole directory with the `TBOT_CONFIG_DIR` environment variable (useful for tests or unusual setups).

### Client data directory

Holds per-settlement `memory/<settlement>/brain.toon`. Resolved by `timberbot.config.data_dir()`:

| Platform | Location |
|---|---|
| Linux | `$XDG_DATA_HOME/timberbot`, falling back to `~/.local/share/timberbot` |
| macOS | `~/Library/Application Support/timberbot` (no separate data convention) |
| Windows | `%LOCALAPPDATA%\timberbot`, falling back to `~/AppData/Local/timberbot` |

Override with the `TBOT_DATA_DIR` environment variable.

### Mod directory

Holds `settings.json` and `state.json`, alongside the mod's `Timberbot.dll` and `manifest.json`:

| Platform | Location |
|---|---|
| Windows | `C:\Users\<you>\Documents\Timberborn\Mods\Timberbot\` |
| macOS, native Linux | `~/Documents/Timberborn/Mods/Timberbot/` |
| Linux + Proton | `~/.steam/steam/steamapps/compatdata/1062090/pfx/drive_c/users/steamuser/Documents/Timberborn/Mods/Timberbot/` |

The build-time `scripts/deploy.sh` honors `TBOT_DOCUMENTS_DIR` when autodiscovering this folder on Proton/Wine setups with a non-`steamuser` username.

## `config.toml` (client)

The `tbot` CLI looks for a TOML file at `<config dir>/config.toml`. A **missing or unparseable file is non-fatal** — the loader returns an empty mapping, the rest of the [resolution chain](#resolution-and-precedence) fills in defaults, and at most one `UserWarning` is emitted per process per error.

Full annotated example with every recognized section:

```toml
# <config dir>/config.toml
#   Linux/macOS: ~/.config/timberbot/config.toml
#   Windows:     %APPDATA%\timberbot\config.toml

[client]
host = "127.0.0.1"        # default target host for the CLI / Python client
port = 8085               # default target port (mod HTTP API)
ws_port = 8086            # default target port (mod WebSocket)
auth_token = ""           # bearer token; required when the mod sets authToken

[backends.claude]         # used by `tbot watch` and `tbot agent run`
model = "claude-opus-4-7"
effort = "high"

[backends.opencode]
model = "glm-4.6"
attach_url = "http://127.0.0.1:4096"   # attach to a long-running `opencode serve`

[backends.custom]
command = "aider --system-prompt-file {skill} {prompt}"   # argv template

[serve]                   # used by `tbot serve` only
backend = "claude"        # or "opencode"
model = "claude-opus-4-7"
acp_binary = "claude-agent-acp"   # ACP agent CLI
mcp_host = "127.0.0.1"
mcp_port = 8091
allowed_tools = ["game.*"]

[serve.telegram]
token = "7912345678:AAH..."   # also: TBOT_TELEGRAM_TOKEN env, --telegram-token flag
allowed_users = [12345678]    # Telegram user IDs allowed to talk to the bot
```

### `[client]`

The connection target shared by every `tbot` command and `TimberbotClient`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host` | string | `127.0.0.1` | Target host for the mod's HTTP API. |
| `port` | int | `8085` | Target port for the mod's HTTP API. |
| `ws_port` | int | `8086` | Target port for the mod's WebSocket (`tbot watch` / `listen` / `serve`). |
| `auth_token` | string | *(none)* | Bearer token sent as `Authorization: Bearer <token>`. Required when the mod sets a non-empty `authToken` (mandatory for a non-localhost `listenAddress`). |

### `[backends.<name>]`

Per-backend defaults fed into `tbot agent run` / `tbot watch`. `<name>` is one of `claude`, `codex`, `opencode`, or `custom`. Explicit CLI flags always win over these defaults.

| Key | Type | Meaning |
|---|---|---|
| `model` | string | Model identifier passed to the backend. |
| `effort` | string | Reasoning effort passed to the backend. |
| `binary` | string | Override the backend's CLI binary path. |
| `command` | string | argv template (required for `custom`); placeholders: `{skill}`, `{instructions_file}`, `{prompt}`, `{prompt_file}`, `{model}`, `{effort}`. |
| `terminal_prefix` | string | Command prefix wrapping the agent invocation; supports `{cwd}` (e.g. `"wt -d {cwd} --"`). |
| `attach_url` | string | URL of a long-running backend server to attach to (opencode only). Set `attach_url = ""` and pass `--attach-url ""` to clear it. |

### `[serve]`

Read **only** by `tbot serve`. It intentionally does *not* fall back to `[backends.*]`, so the interactive and one-shot stacks can pick models and binaries independently. The full flag/env/config matrix is in [Getting Started → `tbot serve` flags](getting-started.md#tbot-serve-flags).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `backend` | string | `claude` | ACP runtime to spawn (`claude` or `opencode`). |
| `model` | string | `claude-opus-4-7` | Model identifier passed to the agent CLI. |
| `acp_binary` | string | `claude-agent-acp` (claude) / `opencode` (opencode) | Path or name of the ACP agent CLI. |
| `mcp_host` | string | `127.0.0.1` | Bind address for the game MCP HTTP/SSE server. |
| `mcp_port` | int | `8091` | Port for the game MCP HTTP/SSE server. |
| `allowed_tools` | list[string] | `["game.*"]` | Glob list of MCP tool names the connector auto-approves; everything else is auto-rejected. |

### `[serve.telegram]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `token` | string | *(required)* | Bot token from `@BotFather`. Also settable via `TBOT_TELEGRAM_TOKEN` or `--telegram-token`. |
| `allowed_users` | list[int] | `[]` | Telegram user IDs allowed to talk to the bot. **Empty/unset means any user who finds the bot can drive it** — `tbot serve` logs a warning at startup. Must be a list of integers or the server refuses to start. |

## Environment variables

Environment variables sit between CLI flags and `config.toml` in the [precedence chain](#resolution-and-precedence).

| Variable | Affects | `config.toml` / flag equivalent |
|---|---|---|
| `TBOT_HOST` | Client target host | `[client].host` / `--host` |
| `TBOT_PORT` | Client target HTTP port | `[client].port` / `--port` |
| `TBOT_WS_PORT` | Client target WebSocket port | `[client].ws_port` / `--ws-port` |
| `TBOT_AUTH_TOKEN` | Bearer token | `[client].auth_token` / `--auth-token` |
| `TBOT_TELEGRAM_TOKEN` | `tbot serve` bot token | `[serve.telegram].token` / `--telegram-token` |
| `TBOT_CONFIG_DIR` | Overrides the [config directory](#client-config-directory) location | — |
| `TBOT_DATA_DIR` | Overrides the [data directory](#client-data-directory) location | — |
| `TBOT_DEBUG` | When set to `1`, `true`, `True`, or `yes`, raises logging to DEBUG | `-vv` / `--debug` |

`TBOT_PORT` / `TBOT_WS_PORT` are parsed as integers; a malformed value emits a `UserWarning` and is ignored (the chain falls through). `TBOT_DOCUMENTS_DIR` and `TBOT_MOD_DIR` are build-time variables for `scripts/deploy.sh` only (overriding the Timberborn Documents directory and the mod folder respectively when deploying a source build), not client settings.

## `settings.json` (mod)

The mod reads `settings.json` from its [mod directory](#mod-directory) at startup. The in-game **Settings** modal is the primary way to change these — `TimberbotService` keeps the values in memory and debounces writes back to disk. Editing the file directly is supported as the advanced/manual path.

The file shipped with the mod sets only the common keys:

```json
{
  "debugEndpointEnabled": false,
  "httpPort": 8085,
  "wsPort": 8086,
  "wsEnabled": true,
  "listenAddress": "127.0.0.1",
  "authToken": "",
  "maxBodyBytes": 1048576
}
```

Every recognized key:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `httpPort` | int | `8085` | HTTP `/api/*` listener port. Values `<= 0` fall back to the default. |
| `wsPort` | int | `8086` | WebSocket `/api/ws` listener port. Values `<= 0` fall back to the default. |
| `wsEnabled` | bool | `true` | When `false`, the WS listener isn't opened; clients fall back to HTTP polling of `/api/agent/state`. |
| `listenAddress` | string | `127.0.0.1` | Bind address for **both** listeners. `+` / `0.0.0.0` binds all interfaces and requires a non-empty `authToken`. |
| `authToken` | string | `""` | Bearer secret. When set, every `/api/*` route except `/api/ping` (and every WS upgrade) requires `Authorization: Bearer <token>`. Mandatory when `listenAddress` is non-loopback — the mod refuses to start otherwise. |
| `maxBodyBytes` | int | `1048576` | Max POST body size in bytes. Negative values fall back to the default. |
| `debugEndpointEnabled` | bool | `false` | Enables debug-only endpoints such as `POST /api/benchmark`. |
| `writeBudgetMs` | double | `1.0` | Per-frame main-thread budget (ms) for draining queued writes. Values `<= 0` fall back to `1.0`. |
| `corsOrigin` | string | `""` | Value echoed in `Access-Control-Allow-Origin`. Empty disables CORS headers. |
| `actionLoggingEnabled` | bool | `true` | Toggles the in-widget action log. |
| `widgetLeft`, `widgetTop` | string | — | Widget on-screen position; written by the widget as you drag it, stored as JSON strings (e.g. `"widgetLeft": "100"`). |

Some runtime settings are applied on load, so changing them may require reloading the save (or the mod) to fully take effect.

!!! note "127.0.0.1 vs localhost"
    Under Mono's `HttpListener` the prefix matches the `Host:` header exactly — a server bound to `127.0.0.1` rejects `Host: localhost` with HTTP 400. Either use `http://127.0.0.1:...` everywhere or set `listenAddress` to `localhost`.

!!! warning "Deprecated keys"
    These keys are read by older mod versions but are now ignored, logging a one-line deprecation notice on load: `terminal`, `pythonCommand`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`, `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `webhookValidateUrls`.

    Backend choice and per-backend model/effort/command defaults now live in [`config.toml`](#configtoml-client) — the connector runs `tbot`, not the mod. Any other unrecognized key (including legacy `agentBinary`, `agentGoal`, `tbotCommand`) is simply ignored without a warning.

## `state.json` (mod)

Agent-shaped state, written next to `settings.json` in the [mod directory](#mod-directory). The widget mutates it through `POST /api/agent/config`; you rarely touch it by hand.

Only three fields are persisted:

```json
{
  "mode": "request",
  "goal": "reach 50 beavers with 77 wellbeing",
  "lastError": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `mode` | string | `"request"` (one dispatch per Launch) or `"autonomous"` (keep dispatching until Stop). |
| `goal` | string | Persistent objective for autonomous mode / the last request prompt. |
| `lastError` | string \| null | Last agent error surfaced to the widget. |

Ephemeral fields — `ready`, `pendingRequest`, `agentStatus`, and the request-id counters — are **in-memory only** and reset to startup defaults on every save load. They appear in the `GET /api/agent/state` response but are never written to disk.

## `brain.toon` (per-settlement memory)

Persistent colony knowledge, one file per settlement, stored in TOON format at `<data dir>/memory/<settlement>/brain.toon`. Managed by `tbot brain` (and the in-game agent during startup); you rarely edit it directly. The settlement name is sanitized for the filesystem. Live summary data is never persisted here — only the goal, tasks, and locations survive between sessions.

```bash
tbot brain                                              # live summary + saved goal/tasks/locations
tbot brain --goal="reach 50 beavers with 77 wellbeing"  # set a persistent goal
```

Logical shape of the stored data (on disk it is serialized via the `toons` library):

```text
timestamp : "2026-05-25T12:04:18.123456"   # ISO timestamp of the last refresh
goal      : "reach 50 beavers with 77 wellbeing"
tasks     :
  - id: 1, status: "pending", action: "build a plank chain"   # status: pending | done | failed
locations :
  dc      : { x: 120, y: 140, z: 2 }
  forest  : { x: 95,  y: 130, z: 0, species: [Pine, Birch] }
  berries : { x: 110, y: 150, z: 0, note: "near the dam" }
```

On first run, `locations` is auto-seeded from live data (district center, up to three tree clusters, up to three food clusters).

!!! note "Legacy location"
    Pre-#43 `brain.toon` files lived under the game's `Documents/Timberborn/Mods/Timberbot/memory/` tree and are migrated to the data directory on first run. If you skipped that upgrade window, copy them across manually: `cp -r <old-mods>/Timberbot/memory/ <data-dir>/memory/`.

## Resolution and precedence

For client settings (`host`, `port`, `auth_token`, …), the first match wins:

| Tier | Where | Owns |
|---|---|---|
| 1. CLI flags | `tbot --host=X --port=Y --auth-token=T` | per-invocation overrides |
| 2. Environment | [`TBOT_*` variables](#environment-variables) | per-shell overrides |
| 3. User config | [`config.toml`](#configtoml-client) | per-user defaults |
| 4. Built-in | hard-coded | `127.0.0.1:8085`, etc. |

Run any command with `-v` to see a `(host=… port=… auth=…)` tag reporting which tier each setting came from (`cli` / `env` / `config` / `default` / `none`) — handy for debugging "why is the client hitting the wrong server" or "why is no auth being sent".

## Agent prompts (`tbot init`)

`tbot init` materializes editable copies of the packaged system prompts into `<config dir>/agent_prompts/`. It is idempotent: without `--force` it leaves your edits alone and only fills in missing files; `--force` overwrites all of them. List the packaged and user-override prompts with `tbot agent prompts`.

Packaged prompts: `auditor`, `connector-mode`, `scout`, `timberbot`, `wirer`. The `timberbot` prompt is the default loaded by `tbot agent run`; the full operating guide behind it is the [Timberbot Guide](timberbot.md).

---

- [Getting Started](getting-started.md) — install and first-run walkthrough
- [API Reference](api-reference.md) — every endpoint, including auth behavior
- [Events](events.md) — WebSocket event stream consumed by `tbot watch` / `listen`
- [Developing](developing.md) — build from source and the mod's settings model
