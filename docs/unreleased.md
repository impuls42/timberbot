## v2 architecture cutover

Hard cutover — no fallback path. The widget no longer spawns the agent; the connector does.

- [feature] **`tbot watch` connector.** Long-running Python process: reconnects with exponential backoff, heartbeats every 2 s, registers a webhook URL for push triggers, dispatches `tbot agent run` (or attaches to `opencode serve`) per cycle.
- [feature] **Launch / Stop ready gate.** The widget button replaces Start/Stop. Until the player presses Launch, the mod returns `409 game_not_ready` on **every `/api/*` read and write** except `/api/agent/*`, `/api/ready`, `/api/tbot/*`, `/api/ping`. Webhooks keep firing.
- [feature] **Mode dropdown.** Request (default) vs Autonomous. Request mode has a per-launch prompt textarea; autonomous mode persists `goal` to `state.json` and lets the connector pick cadence.
- [feature] **Six new endpoints.** `GET /api/agent/state`, `POST /api/agent/config`, `POST /api/agent/request`, `POST /api/ready`, `POST /api/tbot/register`, `POST /api/tbot/heartbeat`.
- [feature] **Bearer-token auth.** `authToken` in `settings.json` requires `Authorization: Bearer <token>` on every `/api/*` route (constant-time compare). The mod refuses to start with a non-localhost `listenAddress` and empty `authToken`. Client side adds `[client].auth_token`, `TBOT_AUTH_TOKEN`, and `TimberbotClient(auth_token=…)`.
- [feature] **`tbot listen` reference receiver.** Standalone aiohttp server for webhook delivery; `--pretty` for human rendering, `--forward-to FILE|URL` for piping.
- [feature] **opencode `--attach`.** `[backends.opencode].attach_url` (or `--attach-url`) makes `tbot agent run` target a long-running `opencode serve` instead of spawning a fresh process.
- [feature] **`state.json`.** New file alongside `settings.json` for agent-shaped state. Persists `mode`, `goal`, `lastError`. Ephemeral state (`ready`, `pendingRequest`, `tbotWebhookUrl`, `lastAckedRequestId`) resets on save load.
- [removed] `TimberbotAgent` subprocess spawn. The mod is a pure HTTP server.
- [removed] Settings keys: `agentBinary`, `agentGoal`, `agentModel`, `agentEffort`, `agentCommandTemplate`, `agentAllowlistEnabled`, `agentAllowedBinaries`, `tbotCommand`. Backend choice lives in `~/.config/timberbot/config.toml`; the connector runs `tbot`, not the mod.
- [docs] [getting-started.md](getting-started.md), [timberbot.md](timberbot.md), [webhooks.md](webhooks.md), and [architecture.md](architecture.md) rewritten around the new flow.

## Error messages

Every API error response is now rich and actionable. The AI gets enough context to correct the next call without guessing.

**Write endpoints (TimberbotWrite.cs)**
- [fix] `not_found` errors explain that ids are ephemeral and tell the caller to re-query buildings
- [fix] `invalid_type` errors include the building name and explain which building types support the operation
- [fix] `invalid_param` errors echo the bad value and list all valid options
- [fix] `no_population` includes requested vs available count
- [fix] `insufficient_science` includes cost and current points with a human-readable message
- [fix] district errors (`not_found`, `SetDistribution`) list all available district names
- [fix] recipe and plantable errors list all available options for that building

**Placement (TimberbotPlacement.cs)**
- [fix] placement validation errors name the blocking object (e.g. "occupied by Lumberjack") instead of generic "occupied"
- [fix] every validation error includes a suggestion (e.g. "demolish it or try a different location")
- [fix] `not_unlocked` tells the caller to use science/unlock first
- [internal] extracted `FindBlockerAt()` helper for DRY blocker lookup across buildings, natural resources, and tracked blockers

**HTTP server (TimberbotHttpServer.cs)**
- [fix] `invalid_body` explains the expected format
- [fix] `unknown_endpoint` now lists all GET and POST endpoints (was only 13, now 55+)

**Python client (timberbot.py)**
- [fix] unknown CLI parameters now show the bad param, valid params, and full usage line
- [fix] toon error output shows the full response dict instead of just the error string

## Storage

- [feature] `POST /api/building/storage` endpoint for piles, warehouses, and tanks
- [feature] set allowed good (`good:Water`) and storage mode (`mode:obtain`) in one call
- [feature] four storage modes matching the player UI: accept, obtain, supply, empty
- [feature] `storageMode` and `allowedGood` fields in buildings detail:full output
- [fix] `SingleGoodAllower` now uses `Allow()` / `Disallow()` instead of direct property set, so the UI updates correctly
- [breaking] `/api/stockpile/good` and `/api/stockpile/capacity` removed, replaced by `/api/building/storage`

## Write endpoint fixes

- [fix] `Workplace.SetDesiredWorkers()` used instead of direct property set, fires `DesiredWorkersChanged` event and handles overstaffing
- [fix] `WorkingHoursManager.WorkedPartOfDay` used instead of direct `EndHours` set, fires `WorkingHoursChangedEvent`
- [fix] `set_workers` minimum clamped to 1 (was 0), matching player UI behavior
- [breaking] `set_capacity` removed, player cannot change stockpile capacity

## Agent

- [removed] shipped Claude Code hooks (`pretool-bash.py`, `session-start.py`) deleted. they blocked parallel tool calls and are no longer needed

## Panel defaults

- [fix] model and effort defaults extracted to named constants (`DefaultClaudeModel`, `DefaultCodexModel`, etc.) instead of scattered string literals
- [fix] switching binary (claude/codex) auto-selects the correct default model and effort

## Testing

- [new] 141 xUnit unit tests for `TimberbotJw` (serialization, commas, nesting, reuse) and `TimberbotPure` (orientation, name cleanup, assertions, normalization, quoting)
- [new] `timberbot/test/` project (net8.0, xUnit) shares source files with the main project, no Unity deps required
- [internal] extracted pure static helpers from 5 Unity-dependent files into `TimberbotPure.cs`

## Docs

- [docs] "Timberbot AI" pages renamed to "Timberbot Guide"
- [docs] getting-started page updated with setup instructions
- [docs] Steam Workshop description rewritten and trimmed to fit the character limit
- [docs] Workshop description mentions Python prerequisite
