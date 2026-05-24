# Game Connector — ACP Integration & User Interaction

| Field | Value |
|---|---|
| Status | Design proposal — partial implementation in `tbot serve` ACP connector (see `python/src/timberbot/user_api/`). |
| Version | 0.1 |
| Scope | Game connector's role as ACP client toward agent runtimes; user-facing API and interaction patterns |
| Out of scope | Game MCP server internals (see *Game Agent Event Delivery*); game runtime implementation; specific agent-runtime internals |
| Companion document | *Game Agent Event Delivery — Tool Result Augmentation* |

---

## 1. Context

### 1.1 The connector's role

The game connector is the stable orchestration point in the gaming agent platform. It owns three boundaries:

- **Toward the user** — exposes a user-facing API for game initiation, observation, input, and control
- **Toward the agent runtime** — acts as ACP client, spawning and driving a swappable agent runtime per session
- **Toward the game** — hosts the game-side MCP server (covered in companion doc) and routes events between the game runtime and active sessions

This document specifies the first two boundaries. The third is the subject of the companion document.

### 1.2 Why ACP

The Agent Client Protocol (ACP) standardizes the interface between a client application and an AI coding/reasoning agent. Choosing ACP gives the platform two properties that matter for this system:

1. **Runtime swappability** — any ACP-compatible agent (Claude Code, OpenCode, Gemini CLI, Codex, Kiro, etc.) can drive a session without changes to the connector
2. **Lifecycle ownership** — the agent runs as a subprocess of the connector; the connector controls spawn, kill, session lifecycle, and capability surface

The alternative (a custom HTTP API per agent) was rejected for the maintenance cost across multiple runtimes and the loss of subprocess-level lifecycle control.

### 1.3 Why a separate user-facing API

ACP is not a user-facing protocol. It is JSON-RPC over stdio between a client process and an agent subprocess. Users — human players — do not speak ACP. The connector must expose its own protocol to users.

A single session may have one initial user (the player who started it) plus zero or more additional observers (spectators, collaborators, operators). All of them connect to the connector via the user-facing API, never directly to the agent runtime.

---

## 2. High-level architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       Game Connector                          │
│                                                               │
│   ┌─────────────────┐         ┌──────────────────────────┐   │
│   │ User-facing API │◀───────▶│   Session Manager        │   │
│   │  (WebSocket)    │         │   (per-session state)    │   │
│   └────────┬────────┘         └────────────┬─────────────┘   │
│            │                               │                  │
│   ┌────────▼────────┐         ┌────────────▼─────────────┐   │
│   │ User Channels   │         │   ACP Client             │   │
│   │ (fan-out hub)   │◀───────▶│   (per session)          │   │
│   └─────────────────┘         └────────────┬─────────────┘   │
│                                            │                  │
│                                            │ JSON-RPC/stdio   │
└────────────────────────────────────────────┼──────────────────┘
                                             │
                                  ┌──────────▼──────────┐
                                  │  Agent runtime      │
                                  │  (subprocess)       │
                                  │  e.g. opencode acp  │
                                  └──────────┬──────────┘
                                             │ MCP
                                             ▼
                                  ┌─────────────────────┐
                                  │ Game MCP server     │
                                  │ (see companion doc) │
                                  └─────────────────────┘
```

### 2.1 Components

| Component | Responsibility |
|---|---|
| User-facing API | Accepts user connections, authenticates, routes to sessions |
| Session Manager | Creates, tracks, and tears down sessions; owns per-session resources |
| User Channels | Per-session pub/sub hub for fan-out to multiple connected users |
| ACP Client | One instance per session; manages the agent subprocess and ACP protocol |
| Agent runtime | External subprocess; varies by configuration |
| Game MCP server | Game-capability surface; details in companion doc |

### 2.2 Session as the unit of orchestration

A *session* is the primary abstraction. One session corresponds to:

- One game instance (one ongoing play)
- One ACP connection to one agent runtime subprocess
- One game MCP server context (events, tools, knowledge)
- Zero or more attached users (one is typical)

Sessions have lifecycle: `pending → active → halting → ended`. State transitions are owned by the Session Manager.

---

## 3. ACP integration design

### 3.1 Subprocess model

The connector spawns the agent runtime as a child process per session:

```
session_start:
  1. Resolve runtime command from session config (e.g., "claude-code-acp", "opencode acp")
  2. Spawn subprocess with stdin/stdout pipes
  3. Initialize ACP handshake over the pipes
  4. Negotiate capabilities
  5. Configure per-session MCP servers
  6. Create ACP session, push initial prompt
  7. Begin reading session/update notifications
```

The subprocess is the connector's child. When the session ends:

```
session_end:
  1. Send ACP session/cancel if still active
  2. Wait for graceful exit (configurable timeout, default 5s)
  3. SIGTERM if still alive
  4. SIGKILL if still alive after 10s
  5. Reap and release resources
```

Process lifecycle is fully owned by the connector. The connector dying takes the agent with it; this is correct behavior for the safety model.

### 3.2 Capability negotiation

At initialization, the connector advertises a deliberately minimal capability surface to the agent:

```json
{
  "protocolVersion": 1,
  "clientCapabilities": {
    "fs": {
      "readTextFile": false,
      "writeTextFile": false
    },
    "terminal": false
  },
  "clientInfo": {
    "name": "game-connector",
    "version": "0.1.0"
  }
}
```

**Filesystem and terminal capabilities are disabled by default.** The agent should not have direct access to either — every game-side action must go through the game MCP server's typed tool surface. This:

- Enforces a clean contract between agent and game
- Prevents the agent from accessing host resources outside the game
- Eliminates an entire class of escape vectors

Per-runtime overrides exist for special-purpose agents (e.g., a debug or analysis agent that needs filesystem read), but the default is locked down.

The agent's capabilities (what it can do) come back from the runtime in the initialize response. The connector logs and validates these but does not gate on them — different runtimes have different strengths.

### 3.3 Session lifecycle in ACP

```
ACP message              Connector behavior
-----------              ------------------
initialize         →     send connector capabilities
                   ←     receive agent capabilities

session/new        →     pass cwd (game workspace) and mcpServers list
                   ←     receive sessionId

session/prompt     →     deliver user prompt + queued game events
                   ←     stream session/update notifications

session/update     →     fan out to attached user channels
(notification)           (text deltas, tool calls, plan updates)

session/request_   →     surface to user channel; await user decision;
permission         ←     respond with allow/deny/cancelled
(from agent)

session/cancel     →     issue on stop signal or user request
                   ←     agent acknowledges; subprocess wind-down

session/load       →     used for resume on connector restart
(optional)               (see §3.7)
```

### 3.4 Initial prompt and event injection

The first `session/prompt` carries:

1. The system-level guidance (delivered as an MCP prompt template; see companion doc §11)
2. The initial game state summary (read from a game MCP resource at session start)
3. The user's opening intent, if any

Subsequent `session/prompt` calls inject:

- New user input (typed/spoken/clicked by attached user)
- Game events the connector classified as "interrupt-worthy" (the cancel-and-reprompt path; see companion doc §11)

Routine game events flow through the MCP envelope (companion doc), not through `session/prompt`. The `session/prompt` channel is reserved for **conversational user input** and **agent-disruptive events** — the high-signal, low-frequency path.

### 3.5 Streaming responses

The agent emits `session/update` notifications during a turn. Notification types include:

- `message_chunk` — token-level text deltas
- `tool_call` — agent invoked a tool (game MCP or built-in)
- `tool_call_update` — progress / completion for a tool call
- `plan_update` — agent revised its plan (some runtimes)
- `thinking` — reasoning content (model-dependent)

The connector forwards these to attached user channels in their original form, after normalizing per-runtime quirks (see §3.8). Users see the agent's activity in near-real-time.

### 3.6 Permission flow

`session/request_permission` is the agent asking the connector for authorization to perform an action. The standard ACP options are `allow_once`, `allow_always`, and `reject_once`.

For the gaming agent, permission requests are surfaced to the controlling user:

```
1. Agent issues session/request_permission with action description
2. Connector publishes a permission request to the controlling user's channel
3. User responds via the user-facing API
4. Connector returns the decision to the agent via ACP

If user does not respond within timeout (default 30s):
  - Default policy applies (per-session config; usually reject_once)
  - Notification of timeout sent to user channel
```

Permission requests are rare in this architecture because the game MCP server's permission model is the primary safety surface. ACP permissions are a backstop for runtime-internal sensitive actions.

### 3.7 Session resume

ACP supports `session/load` for resuming a previous session by ID. The connector exposes this for two scenarios:

- **Connector restart** — when the connector restarts (deploy, crash recovery), active sessions can be resumed by re-spawning the agent runtime and calling `session/load`
- **User reconnect** — when an attached user reconnects, they re-attach to the existing session rather than restarting

For the agent's perspective, `session/load` restores its conversation history. The connector restores its own per-session state (events log, cursor, user attachments) from persistent storage if configured.

Persistent session state is an opt-in deployment choice; see §10.4.

### 3.8 Runtime adapter pattern

Different ACP runtimes have minor protocol divergences. Per the research that informed this design, examples include Kiro using a `prompt` field instead of `content` in some places, and runtimes vary in which `session/update` notification subtypes they emit.

The connector implements a thin adapter layer:

```typescript
interface RuntimeAdapter {
  command(): string[]                              // how to spawn
  buildPrompt(text: string, ...): PromptPayload    // runtime-specific prompt shape
  normalizeUpdate(update: unknown): SessionUpdate  // canonical form
  capabilities: ClientCapabilities                  // overrides if needed
}
```

Adapters live in `connector/runtime-adapters/` with one file per supported runtime. The adapter exposes a canonical interface; the rest of the connector consumes only the canonical form. Adding a new runtime is a matter of adding one adapter file plus tests.

### 3.9 MCP server injection per session

The game MCP server is configured per session via the `mcpServers` field of `session/new`:

```json
{
  "sessionId": "<generated>",
  "cwd": "<game workspace path>",
  "mcpServers": [
    {
      "name": "game",
      "command": "<connector internal MCP endpoint>",
      "args": [],
      "env": {
        "SESSION_ID": "<sessionId>",
        "GAME_INSTANCE": "<game instance id>"
      }
    }
  ]
}
```

In practice the game MCP server is co-located with the connector and is either:

- **Spawned as a child process** of the agent (cleanest isolation; one MCP server process per session)
- **Reached via a local socket** to a long-running connector-internal MCP server (faster startup; shared state managed by session ID)

The second pattern is recommended for production. Subprocess spawn per session has noticeable startup latency in some runtimes.

---

## 4. User-facing API

### 4.1 Transport choice

**Default: WebSocket.** Bidirectional, low-latency, naturally suits the multi-user observation pattern. Single connection per user carries both inbound (user actions) and outbound (agent activity, events, prompts).

**Alternative: SSE + HTTP POST.** Two channels: SSE for connector→user streaming, HTTP POST for user→connector commands. Simpler to proxy through restrictive networks, but doubles the connection management on both sides.

Default decision: WebSocket. SSE+POST is supported as an alternative for clients in environments where WebSocket is unavailable.

### 4.2 Message envelope

All messages, in both directions, use a common envelope:

```typescript
interface UserMessage {
  type: string;        // namespaced: "session.attach", "agent.message_chunk"
  id?: string;         // correlation ID, optional
  session_id?: string; // omitted only for connection-level messages
  ts: string;          // ISO 8601, sender clock
  payload: unknown;    // type-specific
}
```

Connection-level messages (auth, ping/pong) carry no `session_id`. Session-scoped messages always include it; the connector validates the sender is authorized for the named session.

### 4.3 Direction conventions

| Direction | Naming pattern | Examples |
|---|---|---|
| Connector → user | `agent.*`, `session.*`, `game.*`, `system.*` | `agent.message_chunk`, `session.attached`, `game.event` |
| User → connector | `user.*`, `session.command.*` | `user.prompt`, `session.command.cancel` |

This makes log inspection straightforward: a message's name reveals its direction.

### 4.4 Message types — connector → user

| Type | Trigger | Payload summary |
|---|---|---|
| `session.attached` | User attaches to a session | session metadata, current state, role |
| `session.state_change` | Session transitions state | `pending` / `active` / `halting` / `ended` |
| `agent.message_chunk` | ACP `session/update` text delta | text content, chunk index |
| `agent.tool_call` | ACP `session/update` tool_call | tool name, arguments (redacted per policy) |
| `agent.tool_call_update` | ACP `session/update` tool_call_update | tool call id, status, partial result |
| `agent.plan_update` | ACP plan update | plan content |
| `agent.permission_request` | ACP `session/request_permission` | action description, options, timeout |
| `game.event` | High-severity game event the user should see | event content (filtered subset of MCP event log) |
| `game.state_snapshot` | On attach or by request | current game state |
| `system.notice` | Connector-level info or warning | message |
| `system.error` | Recoverable error in connector | error code, message |

### 4.5 Message types — user → connector

| Type | Effect | Payload summary |
|---|---|---|
| `user.attach` | Attach to a session | session_id, optional role |
| `user.detach` | Detach from a session | (none) |
| `user.prompt` | Send a prompt to the agent | text, optional attachments |
| `user.permission_response` | Reply to a permission request | request id, decision (`allow_once`/`allow_always`/`reject_once`) |
| `user.elicitation_response` | Reply to an elicitation from the game MCP | request id, response data |
| `session.command.cancel` | Hard stop the session | reason |
| `session.command.halt` | Request graceful stop via halt advisory | (none) |
| `session.command.create` | Create a new session | game config, initial prompt, runtime preference |

Auth-related messages (handshake, token refresh) live at the connection level and are described in §6.

### 4.6 Backpressure

The connector applies per-user channel buffering. If a user's WebSocket lags (slow consumer), the buffer fills; once it crosses a threshold, the connector either:

- Drops low-priority messages (game events below `notice` severity) — preferred for observers
- Disconnects the user with a backpressure error — for unrecoverably stuck connections

Critical messages (permission requests, halt notifications, session state changes) are never dropped. They are guaranteed delivery within the connection lifetime or trigger a disconnect.

---

## 5. User interaction flows

### 5.1 Starting a session

```
User → Connector:  session.command.create
                   {
                     "game_config": {...},
                     "initial_prompt": "Help me plan an opening strategy",
                     "runtime": "opencode"   // optional; otherwise default
                   }

Connector:         1. Validate auth and quota
                   2. Allocate session ID
                   3. Initialize game instance, get initial game state
                   4. Spawn agent runtime, ACP initialize
                   5. ACP session/new with game MCP config
                   6. ACP session/prompt with system prompt + game state + user prompt

Connector → User:  session.attached
                   { session_id, role: "controller", state: "active" }

Connector → User:  agent.message_chunk (streaming)
                   ...
```

The user becomes the `controller` of the session by virtue of creating it. Additional users may attach as observers with read-only roles.

### 5.2 Observing agent activity

After attachment, the user receives a continuous stream of `agent.*` and `game.event` messages. The client renders:

- `agent.message_chunk` → assistant text in conversation view
- `agent.tool_call` → "calling `game.move`..." indicator with args (per privacy policy)
- `agent.tool_call_update` → result rendering, often as game state change
- `game.event` → ambient notifications in a side panel or log

Tool call arguments may contain user-sensitive information; per-tool redaction policy decides what's surfaced to observers vs. controller.

### 5.3 Providing input mid-session

Users can submit new prompts at any time:

```
User → Connector:  user.prompt
                   { text: "Why did you choose that move?" }

Connector:         1. If session is mid-turn:
                      Queue the prompt for delivery after current turn ends
                      (OR if user marks it urgent, trigger session/cancel + reprompt)
                   2. If session is idle:
                      Send via ACP session/prompt immediately

Connector → User:  agent.message_chunk (streaming response)
```

The connector exposes a `priority` field on `user.prompt`:

- `normal` (default) — queues for delivery after current turn
- `urgent` — triggers `session/cancel` and re-prompts with cancel summary plus the new input

Users see in-flight prompts in a "pending" state until the agent's current turn completes.

### 5.4 Responding to agent permission requests

When the agent issues `session/request_permission`, the controller user receives:

```
Connector → User:  agent.permission_request
                   {
                     id: "<request id>",
                     action: "Read external strategy guide",
                     options: ["allow_once", "allow_always", "reject_once"],
                     timeout_ms: 30000
                   }
```

The client renders a prompt with the three options. User responds:

```
User → Connector:  user.permission_response
                   { id: "<request id>", decision: "allow_once" }
```

The connector relays the decision to the agent via ACP. `allow_always` decisions are persisted per session and applied automatically to subsequent matching requests.

If the timeout elapses without a response, the connector applies the per-session default policy (configurable; typically `reject_once`) and emits a `system.notice` to the user.

### 5.5 Responding to elicitation from game MCP

MCP elicitation (`elicitation/create`) is initiated by a tool the agent called. The agent's tool is paused waiting for the user. The connector surfaces:

```
Connector → User:  game.elicitation_request
                   {
                     id: "<request id>",
                     message: "Choose: left or right at the fork?",
                     schema: { type: "string", enum: ["left", "right"] }
                   }
```

User responds:

```
User → Connector:  user.elicitation_response
                   { id: "<request id>", action: "accept", data: "left" }
```

The connector passes the response back through the MCP elicitation channel; the agent's tool resumes with the user's input baked in.

Elicitation is the preferred mechanism for "the game needs to ask the player something" — it keeps the agent's reasoning structurally aware that a player choice happened, rather than relying on free-text inference.

### 5.6 Stopping a session

Two paths:

**Graceful (`session.command.halt`)** — the connector emits a `system.halt` game event, which surfaces in the next MCP envelope with `advisory: halt`. The agent has one tool call to acknowledge. After acknowledgment or the halt-ack timeout, the connector issues ACP `session/cancel`.

**Hard (`session.command.cancel`)** — the connector immediately issues ACP `session/cancel` and proceeds to subprocess wind-down. Any in-flight agent work is discarded.

```
User → Connector:  session.command.halt   (or .cancel)
Connector → User:  session.state_change { state: "halting" }
...
Connector → User:  session.state_change { state: "ended" }
```

The hard path is used for safety stops and user-requested immediate termination. The graceful path is used for normal end-of-session and operator-initiated shutdowns.

### 5.7 Reconnecting

If a user's WebSocket drops, they reconnect with the same session ID:

```
User → Connector:  user.attach
                   { session_id: "<existing>", since_seq?: 12345 }

Connector → User:  session.attached
                   { session_id, role, state, replay_from: 12345 }

Connector → User:  (replays missed messages from buffer)
                   ...
```

The connector maintains a bounded per-user-channel replay buffer (default: last 200 messages) for short reconnects. Beyond that window, the user receives a state snapshot instead of full replay.

---

## 6. Authentication and authorization

### 6.1 Authentication

Recommended: OIDC for user authentication, with the connector accepting bearer tokens issued by an external identity provider. The connector validates tokens on every WebSocket connection and on every HTTP request to the user-facing API.

Token presentation:

- WebSocket: `Authorization: Bearer <token>` header at connection open, or a `connection.auth` message immediately after open
- HTTP: standard `Authorization` header

The specific identity provider is a deployment concern, not a design concern. The connector's auth layer is provider-agnostic.

### 6.2 Authorization model

Three role concepts per session:

| Role | Permissions |
|---|---|
| `controller` | Full: create, prompt, permission-respond, halt, cancel |
| `participant` | Limited: prompt, elicitation-respond (if game allows); no admin commands |
| `observer` | Read-only: receive agent and game messages; no input |

The session creator is the default controller. Additional roles are granted via session sharing (out of scope for v1; a follow-up may add a sharing protocol).

### 6.3 Per-tool privacy policy

Some tool call arguments (e.g., user input passed through a tool) may be sensitive. The connector applies a per-tool redaction policy when fanning out to non-controller users:

```json
{
  "tools": {
    "game.send_private_message": {
      "redact_args": ["message_text"],
      "redact_for_roles": ["participant", "observer"]
    }
  }
}
```

Controllers see the unredacted form. Observers see a placeholder. This prevents the multi-user observation feature from becoming a privacy leak.

---

## 7. Multi-user fan-out

### 7.1 User Channels

The connector maintains a per-session pub/sub hub. Each attached user has a channel; messages from the session are published once and fanned out.

```
Session activity ──▶ Channel publisher ──┬──▶ User A channel ──▶ WebSocket A
                                          ├──▶ User B channel ──▶ WebSocket B
                                          └──▶ User C channel ──▶ WebSocket C
```

Each channel has:

- Per-user filter (based on role)
- Replay buffer (last N messages for reconnect)
- Backpressure state

### 7.2 Filtering

Messages are filtered per channel based on the attached user's role. The filter is applied at publish time:

| Message type | Controller | Participant | Observer |
|---|---|---|---|
| `agent.message_chunk` | full | full | full |
| `agent.tool_call` (sensitive args) | full | redacted | redacted |
| `agent.permission_request` | yes | no | no |
| `game.elicitation_request` | yes | maybe (per policy) | no |
| `game.event` (severity ≥ notice) | yes | yes | yes |
| `system.*` | yes | yes | yes |

Permission and elicitation requests target the role configured to respond — usually controller, sometimes participant for elicitation if the game design allows.

### 7.3 Conflict resolution

If multiple controllers somehow exist (e.g., role escalation by an admin operator), input commands are processed in arrival order. The agent sees a single coherent stream of prompts; the multi-controller situation is invisible to the agent.

For permission and elicitation responses, the first response wins. Subsequent responses for the same request ID are ignored with a `system.notice` to the late responder.

---

## 8. Configuration

### 8.1 Connector-level config

```json
{
  "connector": {
    "listen": { "ws": "0.0.0.0:8443", "tls": true },
    "auth": { "provider": "oidc", "issuer": "https://idp.example/", "audience": "game-connector" },
    "session_defaults": {
      "halt_ack_timeout_ms": 10000,
      "permission_timeout_ms": 30000,
      "permission_default_on_timeout": "reject_once",
      "user_prompt_default_priority": "normal",
      "replay_buffer_size": 200,
      "channel_backpressure_threshold": 64
    }
  }
}
```

### 8.2 Per-session config

Passed at `session.command.create`:

```json
{
  "runtime": "opencode",
  "game_config": { ... },
  "agent_capabilities_override": {
    "fs": { "readTextFile": false, "writeTextFile": false },
    "terminal": false
  },
  "game_events": { ... see companion doc ... },
  "user_input_priority_allowed": ["normal", "urgent"],
  "permission_policy": {
    "default_on_timeout": "reject_once",
    "always_decisions_persist": true
  }
}
```

### 8.3 Per-runtime adapter config

Each runtime adapter ships with default command, args, and capability overrides. Operators may override per deployment.

```json
{
  "runtimes": {
    "opencode": {
      "command": ["opencode", "acp"],
      "env": {},
      "capability_overrides": {}
    },
    "claude-code": {
      "command": ["claude-code-acp"],
      "env": {},
      "capability_overrides": {}
    },
    "gemini": {
      "command": ["gemini", "--experimental-acp"],
      "env": {},
      "capability_overrides": {}
    }
  }
}
```

---

## 9. Failure modes

| Failure | Mitigation |
|---|---|
| Agent runtime crashes mid-session | Detect SIGCHLD; mark session as `failed`; notify users; offer resume via `session/load` if persistence enabled |
| Agent runtime hangs | Watchdog timer on session/update notifications; if no progress for N minutes during expected activity, escalate to `session/cancel` + restart attempt |
| User WebSocket disconnect | Replay buffer holds recent messages; reconnect within window resumes seamlessly |
| All users disconnect | Session continues unless configured otherwise; agent activity continues; on next attach, user catches up via replay or snapshot |
| User submits malformed message | Reject with `system.error`; do not affect session |
| Auth token expires mid-session | Connector requests refresh via `connection.auth_refresh`; if not provided within grace period, disconnect (session remains active for reconnect) |
| ACP protocol version mismatch | Negotiate to highest common version; log warning if non-standard version chosen |
| Runtime-specific quirk causes adapter failure | Adapter logs the divergence; falls back to canonical form where possible; otherwise session ends with operator-actionable error |
| MCP server crash | Detected via MCP transport error; session continues but tool calls fail; operator alerted; manual restart or session end |
| Permission/elicitation timeout | Default policy applied; user notified; session continues |
| Halt acknowledgment not received | Escalates to hard `session/cancel` after `halt_ack_timeout_ms` |
| Connector restart with active sessions | If persistence enabled: re-spawn agents, `session/load`, reconnect users via attach. If not: sessions are lost; users informed on next connect |

---

## 10. Implementation plan

### 10.1 Phase 1 — ACP plumbing for one runtime

**Goal:** Connector can spawn one specific agent runtime, run a session end-to-end, deliver streamed output to a single user.

- Subprocess management (spawn, stdio plumbing, reap)
- ACP initialize and capability negotiation
- `session/new` with hardcoded MCP server config
- `session/prompt` with hardcoded initial prompt
- `session/update` ingestion and routing to a single user channel
- `session/cancel` on user request
- Minimal user-facing WebSocket with attach + prompt + cancel messages
- One runtime adapter (suggest: OpenCode for stability)

**Exit criteria:** A test harness can spawn the connector, attach a WebSocket client, start a session, see agent streaming output, send a follow-up prompt, and cancel.

### 10.2 Phase 2 — Multi-runtime support and capabilities

- Runtime adapter pattern formalized
- Two more runtime adapters (Claude Code, Gemini CLI)
- Capability negotiation with override per-runtime
- `session/request_permission` flow end-to-end
- Privacy policy framework (per-tool redaction)

**Exit criteria:** Same test can be run against three different runtimes by config change only; permission requests round-trip correctly.

### 10.3 Phase 3 — Multi-user fan-out and roles

- User Channels pub/sub hub
- Role model (controller, participant, observer)
- Filtering per channel
- Replay buffer for reconnects
- Backpressure handling

**Exit criteria:** Multiple WebSocket clients can attach to one session with different roles; reconnection resumes cleanly; backpressure on one user doesn't affect others.

### 10.4 Phase 4 — Persistence and resume

- Per-session state persistence (event log, cursor, attachments)
- `session/load` on agent re-spawn after connector restart
- Operational tooling for inspecting persisted sessions

**Exit criteria:** Connector restart preserves active sessions; users reconnect; agents resume conversation history; event log replays correctly.

### 10.5 Phase 5 — Production hardening

- Auth integration (OIDC)
- Per-deployment runtime adapter registry
- Observability (metrics, structured logs, traces)
- Rate limiting and quota
- Operational runbooks for common failure modes

**Exit criteria:** Connector can be deployed behind a production gateway; operators can diagnose typical failures from logs and metrics; resource consumption is bounded per session.

---

## 11. Open questions

1. **Session sharing protocol.** How do controllers grant access to other users? Magic link? Token issued to specific user? Out of scope for v1; needs design before multi-user becomes a primary feature.

2. **Concurrent session limits per user.** Should one user be able to run N sessions in parallel? Quota model? Likely needed before public deployment.

3. **Runtime cost accounting.** Different runtimes have different pricing models (per-token, per-session, free local). Should the connector track and report cost per session? Recommend yes, even minimally, for operational visibility.

4. **Permission persistence scope.** `allow_always` decisions persist per session today. Should they persist across sessions for the same user? Probably yes for trust-elevated users, no for general users.

5. **Tool call argument redaction defaults.** Out of the box, what's the safe default for arguments — show all to controller, redact all to others, or have an allowlist? Recommend allowlist (opt-in to share) for safety.

6. **Reconnect window vs. replay buffer.** Currently coupled (buffer holds last N messages regardless of time). Should be both time-bounded and count-bounded? Likely yes; configurable.

7. **Runtime adapter test suite.** Each adapter should pass a canonical test suite to be considered supported. What's in the suite? Suggest: initialize, session creation, prompt, streamed response, tool call, permission request, cancel. Build this alongside the third adapter.

---

## 12. References

### Protocol specifications

- Agent Client Protocol — https://agentclientprotocol.com
- ACP Schema — https://agentclientprotocol.com/protocol/schema
- Model Context Protocol — https://modelcontextprotocol.io
- OAuth 2.1 / OIDC

### Companion documents

- *Game Agent Event Delivery — Tool Result Augmentation* (referenced throughout)

### Related architectural decisions

- ADR-001 (assumed): Adoption of ACP for swappable agent runtimes
- ADR-002 (assumed): Game MCP server as the typed game-capability surface
- ADR-003 (assumed): Game connector as orchestrator and ACP client

### Background reading

- ACP agent registry — https://agentclientprotocol.com/get-started/agents
- Multi-agent fan-out patterns
- Voice agent barge-in interaction model (analogous user-interruption pattern)

---

## Appendix A — Message sequence: session start to first agent response

```
User                Connector              ACP/Agent             Game MCP
 │                     │                      │                     │
 │ user.attach (or)    │                      │                     │
 │ session.command.    │                      │                     │
 │ create              │                      │                     │
 ├────────────────────▶│                      │                     │
 │                     │ spawn subprocess     │                     │
 │                     ├─────────────────────▶│                     │
 │                     │ initialize           │                     │
 │                     │◀────────────────────▶│                     │
 │                     │ session/new          │                     │
 │                     │  (mcpServers=[game]) │                     │
 │                     ├─────────────────────▶│ MCP initialize      │
 │                     │                      ├────────────────────▶│
 │                     │                      │◀────────────────────┤
 │                     │ session/prompt       │                     │
 │                     │  (system + state +   │                     │
 │                     │   user prompt)       │                     │
 │                     ├─────────────────────▶│                     │
 │                     │                      │ tools/call          │
 │                     │                      ├────────────────────▶│
 │                     │                      │◀────────────────────┤
 │ session.attached    │                      │                     │
 │◀────────────────────┤                      │                     │
 │                     │ session/update       │                     │
 │ agent.message_chunk │ (message_chunk)      │                     │
 │◀────────────────────┤◀─────────────────────┤                     │
 │  ...streaming...    │                      │                     │
```

## Appendix B — Pseudocode: ACP client core

```python
class ACPClient:
    def __init__(self, session_id, runtime_adapter, mcp_config, channel_hub):
        self.session_id = session_id
        self.adapter = runtime_adapter
        self.mcp_config = mcp_config
        self.hub = channel_hub
        self.proc = None
        self.acp_session_id = None

    async def start(self, initial_prompt):
        self.proc = await spawn(self.adapter.command(), pipe_stdio=True)
        asyncio.create_task(self._read_updates())

        await self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": self.adapter.capabilities,
            "clientInfo": {"name": "game-connector", "version": VERSION},
        })

        result = await self._rpc("session/new", {
            "cwd": self.mcp_config.cwd,
            "mcpServers": [self.mcp_config.spec],
        })
        self.acp_session_id = result["sessionId"]

        await self._rpc("session/prompt",
            self.adapter.buildPrompt(initial_prompt, self.acp_session_id))

    async def prompt(self, text, priority="normal"):
        if priority == "urgent" and self._is_busy():
            await self.cancel()
        await self._rpc("session/prompt",
            self.adapter.buildPrompt(text, self.acp_session_id))

    async def cancel(self):
        await self._rpc("session/cancel", {"sessionId": self.acp_session_id})

    async def _read_updates(self):
        async for msg in self.proc.stdout_jsonrpc():
            if msg.method == "session/update":
                canonical = self.adapter.normalizeUpdate(msg.params)
                await self.hub.publish(self.session_id, canonical)
            elif msg.method == "session/request_permission":
                await self._handle_permission(msg)
            # ... other notifications

    async def shutdown(self, grace_ms=5000):
        try:
            await self.cancel()
            await asyncio.wait_for(self.proc.wait(), grace_ms / 1000)
        except asyncio.TimeoutError:
            self.proc.terminate()
            await asyncio.wait_for(self.proc.wait(), 10)
            if self.proc.returncode is None:
                self.proc.kill()
```

---

*End of document.*
