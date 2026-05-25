# Subagent Delegation — `mcp__game__delegate` and the Subagent Lifecycle

| Field | Value |
|---|---|
| Status | Phase 1 implemented on 2026-05-25 — `AgentConnection`/`Session` split, `SubagentRegistry`, 7 delegate-family MCP tools, user_id header routing, eviction wiring. Phase 2 and 3 outstanding. Supersedes the materialized `.claude/agents/` approach that was reverted on 2026-05-25. |
| Version | 0.2 |
| Scope | A code-defined fleet of specialized subagents driven by the main `tbot serve` agent through MCP tools. Multi-session ACP connection management. |
| Out of scope | Cross-user subagent sharing; warm-pool of pre-spawned agent runtimes; per-subagent model selection (all subagents inherit the main session's model). |
| Companion documents | *Game Connector — ACP Integration & User Interaction*; *Game Agent Event Delivery — Tool Result Augmentation* |

---

## 1. Motivation

### 1.1 What today's `tbot serve` looks like

A single Telegram user owns one main ACP session running in one `claude-agent-acp` subprocess. The main agent has access to every `mcp__game__*` tool and is expected to drive the entire conversation, including any narrowly-scoped subtask, by itself.

That works, but it has three real costs:

- **Context budget waste.** A single `prefabs` read dumps ~28 KiB of JSON into the main agent's context, blowing past the useful working set for the rest of the session. Every audit and every placement search pays this tax.
- **Permission scoping is all-or-nothing.** The main agent has `place_building` available at every turn, which means a stray "delete that pump" while it was meant to be exploring can do real damage. There's no way to say "this turn is read-only".
- **No parallelism.** Independent operations (validate a Lumberjack spot AND audit alerts AND check the automation graph) execute sequentially in one long monologue.

### 1.2 Why the previous attempt didn't fit

We previously implemented Option A — materializing subagent definitions as `.claude/agents/*.md` and `.opencode/agent/*.md` files so the main agent could call Claude Code's native `Task` tool. Two limitations made it the wrong default:

1. The delegation is *opaque* to the connector. We can't observe the subagent's tool calls, can't time out, can't reuse a subagent across multiple of the main agent's turns. The runtime owns the entire lifecycle.
2. The frontmatter formats diverge between runtimes (Claude Code's `tools:` list vs. OpenCode's `permission:` map). A code-level spec has to be rendered into both, and the MCP-scoping guarantees are different on each side.

### 1.3 What this design proposes

A typed MCP tool family — `delegate`, `subagent_reply`, `subagent_status`, `subagent_wait`, `subagent_wait_all`, `subagent_cancel`, `subagent_close`, `subagent_list`, `subagent_transcript` — that the main agent invokes through the same `game` MCP server it already uses. The connector owns each subagent's full lifecycle: spawn, status, follow-ups, cancellation, cleanup.

Subagents are **persistent across multiple main-agent turns**. The main agent gets back a stable `subagent_id` it can hold onto, reply to as if it were the user, peek at, or dismiss. Multiple subagents can be running concurrently, all driven from the user's single existing `claude-agent-acp` process.

---

## 2. Architecture

### 2.1 One process, many sessions

The Agent Client Protocol explicitly supports many sessions per connection — every ACP method takes `session_id` as a parameter, and there is a `session/close` operation. Today's `SessionHandle` conflates the connection (the subprocess + stdio) with one session into a single object. The proposed design splits them:

```
AgentConnection                                 (one claude-agent-acp subprocess)
├── _conn        — acp.ClientSideConnection     (one stdio pipe)
├── _client      — _ConnectorClient             (dispatches session/update by sid)
└── _sessions    — dict[session_id, Session]    (many)

Session                                         (one conversation thread)
├── session_id
├── allowed_tools                               (per-session scope; enforced via request_permission)
├── on_update / on_elicitation / on_tool_action (per-session callbacks)
├── _emitted_tool_calls / _tool_call_meta       (per-session dedup state)
└── _current_turn — asyncio.Future[str] | None  (tracks in-flight prompt)
```

Inbound `session/update` notifications carry `session_id`; `_ConnectorClient.session_update` looks up the right `Session` and fires its callbacks. `request_permission` checks the calling session's `allowed_tools` rather than a process-wide list.

### 2.2 Process map under load

For a user with three concurrent delegations active:

```
User Alice
└── AgentConnection (one claude-agent-acp #1)
    ├── Session  main-alice          ← long-lived chat session
    ├── Session  scout-a8f3   (idle)        ← awaiting next reply
    ├── Session  wirer-d4e1   (running)     ← turn in flight
    └── Session  auditor-12c7 (completed)   ← reply buffered

User Bob
└── AgentConnection (one claude-agent-acp #2)
    └── Session  main-bob
```

Process isolation is between users; session multiplexing is within each user. A subagent cold start is now a `session/new` JSON-RPC round-trip (≈ tens of ms), not a Node.js startup (~3-5 s).

### 2.3 Trade-offs accepted

| Aspect | Position |
|---|---|
| Crash blast radius | One subprocess hosts a user's main + all their subagents. If it dies, everything for that user dies. Acceptable: if the main is dead the user has to retry anyway. |
| Concurrency model | Anthropic models may emit multiple tool calls in one turn. Each `delegate(..., wait=False)` returns immediately, so the main agent can fan out N delegations and pick them up later via `subagent_wait_all`. |
| Token accounting | Each session has its own context window. Subagent tokens are independent of the main agent's — that's the whole point. |
| Model homogeneity | All sessions inherit the main session's pinned model. A subagent cannot run on a cheaper model in v0.1. (See §10.) |

---

## 3. The `AgentSpec` data model

`AgentSpec` is already defined in `connector/agent_spec.py` and stays mostly as-is. Today's instances:

| Instance | Slug | Allowed MCP tools | Notes |
|---|---|---|---|
| `TIMBERBOT_SPEC` | `timberbot` | every read + every write | the main agent — bootstrap is rendered via `render_bootstrap_prompt` |
| `WIRER_SPEC` | `wirer` | `summary`, `buildings`, `districts`, `link`, `unlink`, `configure_automation`, `rename_automation` | mutates only automation graph |
| `SCOUT_SPEC` | `scout` | inspection reads + `find_placement`, `find_planting`, `building_range` | read-only from the world's POV |
| `AUDITOR_SPEC` | `auditor` | every read tool | strictly no mutations |
| `SUBAGENTS` | — | tuple of the three above | iteration order |

`SUBAGENTS` is the registry of delegation targets. Adding a new subagent is one Python edit.

The `slug` field is the public name the main agent uses in `delegate(agent="<slug>", ...)`. The 4-byte nonce gets appended at runtime to form a unique `subagent_id` (`scout-a8f3`).

---

## 4. Subagent identity and storage

### 4.1 ID format

`<slug>-<nonce>` where `nonce` is 4 lowercase hex chars (so `2^16 = 65 536` IDs per slug per user before a collision risk worth mentioning).

- Readable in chat and logs: `scout-a8f3`
- Cheap for the model to echo back in tool calls
- Per-user uniqueness only — two different users can both have a `scout-a8f3`

On collision, the registry retries with a fresh nonce. Three retries before raising.

### 4.2 `SubagentRun` and `SubagentRegistry`

```python
@dataclass
class SubagentRun:
    subagent_id: str
    spec: AgentSpec
    session: Session                       # the underlying ACP session
    status: Literal[
        "idle", "running", "completed",
        "errored", "cancelled", "closed",
    ]
    transcript: list[Turn]                 # all (user_msg, agent_reply) pairs
    current_turn: asyncio.Future[str] | None   # set while a turn is in flight
    last_error: str | None
    created_at: float                      # monotonic
    last_active_at: float


@dataclass
class Turn:
    user_message: str
    agent_reply: str
    stop_reason: str                       # end_turn / max_tokens / refusal / cancelled
    started_at: float
    ended_at: float


class SubagentRegistry:
    """One per user — keyed off the same user_id that maps to an AgentConnection."""

    _runs: dict[str, SubagentRun]          # subagent_id -> run

    async def open(self, spec: AgentSpec, conn: AgentConnection) -> SubagentRun: ...
    def get(self, subagent_id: str) -> SubagentRun | None: ...
    async def close(self, subagent_id: str) -> None: ...
    def list(self) -> list[SubagentRun]: ...
```

A single global object `subagent_registries: dict[user_id, SubagentRegistry]` lives alongside the existing `_connections: dict[user_id, AgentConnection]` in the serve module.

### 4.3 Status state machine

```
                ┌────────────┐
                │            │
                │   idle     │◀──────────────────────┐
                │            │                       │
                └──────┬─────┘                       │
                       │ delegate / subagent_reply   │
                       ▼                             │
                ┌────────────┐    end_turn           │
                │  running   │───────────────────────┤
                └──────┬─────┘                       │
                       │ stop_reason ∉ end_turn      │
                       │ (max_tokens / refusal)      │
                       ▼                             │
                ┌────────────┐                       │
                │ completed  │───── subagent_reply ──┘
                └──────┬─────┘
                       │ subagent_close
                       │
              ┌────────┴─────┐
              ▼              ▼
        ┌──────────┐   ┌──────────┐
        │ closed   │   │ errored  │   (set on subprocess crash / ACP error;
        └──────────┘   └──────────┘    subagent_close moves it to "closed")

cancelled is reachable from running on subagent_cancel; it acts like completed
for follow-up purposes (the agent may issue a fresh subagent_reply).
```

---

## 5. MCP tool surface

All tools live in the `game` MCP server (`game_mcp/server.py`) and return the standard `{result, meta}` envelope. The shapes below describe the `result` block; `meta` is unchanged from existing tools.

### 5.1 `delegate(agent: str, task: str, wait: bool = False)`

Open a new subagent run and start the first turn.

**Default `wait=False`.** Returns immediately with the `subagent_id` and `status="running"`. The agent can poll via `subagent_status`, fetch via `subagent_wait`, or batch via `subagent_wait_all`.

When `wait=True`, blocks until the first turn ends and returns `{subagent_id, status, stop_reason, reply}`.

```jsonc
// wait=False (default)
{ "subagent_id": "scout-a8f3", "status": "running" }

// wait=True
{ "subagent_id": "scout-a8f3",
  "status": "completed",
  "stop_reason": "end_turn",
  "reply": "Coords: (66, 70, 4) facing south. ..." }
```

### 5.2 `subagent_reply(subagent_id: str, message: str, wait: bool = False)`

Send a follow-up turn. From the subagent's perspective this is the user's next message, so its prior turns (every earlier `task` / `message` and the corresponding replies) are all still in scope.

Rejects if a turn is already in flight on that session:

```jsonc
{ "subagent_id": "scout-a8f3", "error": "busy", "status": "running" }
```

Otherwise behaves like `delegate` from there.

### 5.3 `subagent_status(subagent_id: str)`

Cheap non-blocking peek. Returns metadata only — no reply text.

```jsonc
{ "subagent_id": "scout-a8f3",
  "agent": "scout",
  "status": "running",
  "turns_completed": 1,
  "last_active_at": 1779712345.0 }
```

### 5.4 `subagent_wait(subagent_id: str, timeout: float = 60.0)`

Block until the in-flight turn finishes (or timeout). If the subagent is already idle/completed/errored, returns the last reply immediately.

```jsonc
{ "subagent_id": "scout-a8f3",
  "status": "completed",
  "stop_reason": "end_turn",
  "reply": "...",
  "timed_out": false }
```

### 5.5 `subagent_wait_all(timeout: float = 60.0)`

Block until **every** in-flight subagent (for the calling user) reaches a non-running state, or timeout. Returns an array of `{subagent_id, status, reply}` results in the order they completed.

The intended workflow is: the main agent fires several `delegate(..., wait=False)` calls early in its turn, then issues one `subagent_wait_all` and processes the batch when control returns.

```jsonc
{ "results": [
    { "subagent_id": "scout-a8f3",  "status": "completed", "reply": "..." },
    { "subagent_id": "wirer-d4e1",  "status": "completed", "reply": "..." },
    { "subagent_id": "auditor-12c", "status": "errored",   "last_error": "tool timed out" }
  ],
  "timed_out": false }
```

If a subagent is `idle` (no turn in flight when `wait_all` is called) it's reported as-is with whatever its current state is — `wait_all` never *starts* a turn, it only waits for one already in progress.

### 5.6 `subagent_cancel(subagent_id: str)`

Cancel the in-flight turn via ACP `session/cancel`. The session stays open; the agent may issue another `subagent_reply` to redirect it. Returns `{status: "cancelled"}`.

### 5.7 `subagent_close(subagent_id: str)`

Release the session (ACP `session/close`) and drop from the registry. After this the id is invalid. Returns `{ok: true}`.

### 5.8 `subagent_list()`

Returns every subagent currently registered for the calling user, in `created_at` order:

```jsonc
{ "subagents": [
    { "subagent_id": "scout-a8f3",  "agent": "scout",   "status": "idle",      "turns_completed": 2, "last_active_at": 1779712345.0 },
    { "subagent_id": "wirer-d4e1",  "agent": "wirer",   "status": "running",   "turns_completed": 0, "last_active_at": 1779712390.0 },
    { "subagent_id": "auditor-12c", "agent": "auditor", "status": "completed", "turns_completed": 1, "last_active_at": 1779712301.0 }
  ] }
```

### 5.9 `subagent_transcript(subagent_id: str)`

Return the full conversation history of the subagent — every `(user_message, agent_reply)` pair so far. Intended for edge cases where the main agent needs to re-read what was discussed (e.g. after a context reset, or to extract structured data from an earlier turn).

```jsonc
{ "subagent_id": "scout-a8f3",
  "agent": "scout",
  "status": "idle",
  "turns": [
    { "user_message": "Find a Lumberjack spot near the trees.",
      "agent_reply": "Best candidate: (66, 70, 4) facing south.",
      "stop_reason": "end_turn" },
    { "user_message": "Anything closer to the DC?",
      "agent_reply": "There's (72, 71, 4) but tighter tree access.",
      "stop_reason": "end_turn" }
  ] }
```

Heavier payload than the other tools — instructions discourage routine use.

---

## 6. Lifetime, cleanup, and edge cases

| Trigger | Behavior |
|---|---|
| Main user `/cancel` or `/halt` | **Phase 1:** treated as full eviction — cancel the main turn, then cancel + close every subagent for that user and tear down the `AgentConnection`. Next user message reconnects from scratch. **Phase 2 (planned):** cancel turns only; keep sessions open so the agent can revive them on its next message. The softer semantic was the original §1.3 goal but is deferred because the current `_user_message_loop` always evicts on cancel — splitting that out is a separate change. |
| Main session evicted (handle dies, ENDED state) | Cancel **and** close every subagent for that user. Next user message opens a fresh main session with an empty registry. |
| Subagent stop_reason ≠ `end_turn` | Status moves to `completed` (with the actual stop reason recorded) for `max_tokens` / `refusal`, or `cancelled` for `cancelled`. The reply text is still buffered for retrieval. |
| Subagent process or session crashes mid-turn | `status="errored"`, `last_error` populated. Session retained until explicit `subagent_close` so the agent can inspect what happened. |
| Collision on `subagent_id` nonce generation | Retry with fresh nonce, up to 3 attempts. Vanishingly rare. |
| `subagent_reply` while a turn is in flight on the same session | Reject `{error: "busy"}`. The main agent must `subagent_wait` or `subagent_cancel` first. |
| Idle subagent past idle-timeout threshold | Auto-close — see §6.1. |
| `tbot serve` shutdown | TaskGroup teardown closes the `AgentConnection`, which cancels all turns and closes all sessions. |

### 6.1 Idle timeout

Each `SubagentRun` records `last_active_at` (updated on every `delegate`, `subagent_reply`, `subagent_wait`, `subagent_status` *for the targeted run*). A background task in the registry walks all runs every 30 s; any run whose status is `idle | completed | errored` and whose `last_active_at` is older than `SUBAGENT_IDLE_TIMEOUT` (default **600 s** = 10 minutes) is closed.

Configurable per-serve via `ServeConfig.subagent_idle_timeout_s`. Running sessions are never auto-closed, regardless of `last_active_at`.

---

## 7. The `user_id` discovery problem

The `delegate` MCP tool handler runs inside the FastMCP server in a request context that knows the SSE session id but not the Timberbot `user_id`. We need to map one to the other so the handler can pick the right `AgentConnection` and `SubagentRegistry`.

**Approach (a) — explicit mapping table.** When `_user_message_loop` opens the main ACP session, it also records `mcp_session_id → user_id` in a process-global map. The `delegate` handler reads its FastMCP request context, pulls the SSE session id, and looks it up.

Sketch:

```python
# at MCP server creation
USER_BY_MCP_SESSION: dict[str, str] = {}

# at main-session open in _user_message_loop
mcp_session_id = await register_user_with_mcp(client, mcp, user_id)
USER_BY_MCP_SESSION[mcp_session_id] = user_id

# in the delegate tool handler
def _calling_user_id(ctx: fastmcp.Context) -> str:
    return USER_BY_MCP_SESSION[ctx.session_id]
```

The cleanest place to register is at `new_session(mcp_servers=...)` time — when the MCP client (the agent runtime) first connects, FastMCP emits a connect hook with the session id. We bind it to the user_id that triggered the `new_session` call.

Risks: a stale entry if the MCP session outlives the user's main agent session. Mitigation: drop the entry on `Session.close()`.

(b) — passing `user_id` as an explicit MCP tool argument — and (c) — running a separate MCP server per user — are both possible but worse, the first because it leaks identity into the prompt, the second because it multiplies the moving parts.

---

## 8. Bootstrap prompts

Two distinct prompts:

### 8.1 Main agent bootstrap

Add a §"Delegation" block to `render_bootstrap_prompt` that lists the available subagents and explains the full lifecycle (delegate → status / wait → reply → close). Stays runtime-neutral — calls are always `mcp__game__<tool>(...)`, never `Task(...)`.

Verbatim text (subject to revision before implementation):

> ### Delegating to subagents
>
> You have three specialist subagents. Each one is a separate conversation you can iterate with across multiple of your own turns:
>
> - `scout` — placement validation. Returns coordinates; never places.
> - `wirer` — automation graph edits. Applies link/unlink/configure diffs.
> - `auditor` — read-only state inspection. Filters and summarizes.
>
> **Start one** with `mcp__game__delegate(agent="<slug>", task="<initial instructions>")`. Returns a `subagent_id` immediately; the subagent is running in the background.
>
> **Follow up** with `mcp__game__subagent_reply(subagent_id=…, message=…)` — the subagent sees this as the user's next message and replies with full prior context.
>
> **Pick up results** with `mcp__game__subagent_wait(subagent_id=…)` (blocks on one) or `mcp__game__subagent_wait_all()` (blocks on all in-flight at once — preferred when you fanned out several delegations).
>
> **Inspect** with `subagent_status` (cheap peek), `subagent_list` (all your active subagents), or `subagent_transcript` (full history — heavy, use sparingly).
>
> **Stop** with `subagent_cancel` (interrupt current turn, keep session) or `subagent_close` (dismiss the subagent permanently).
>
> Prefer fanning out several `delegate(...)` calls when tasks are independent, then collecting with one `subagent_wait_all`. Sequential `delegate(..., wait=True)` calls waste opportunity.

### 8.2 Subagent bootstrap

A separate `render_subagent_bootstrap(spec)` that drops the Telegram framing and the "continuity across many turns" rule (the subagent doesn't know it has many turns; it just answers what's asked). Carries identity, allowed-tool list, refusal sentence, and behavior rules — same structural blocks as the main bootstrap, no §"Delegation" section.

This is a draft surface; revisit when implementing Phase 1.

---

## 9. Phasing

### 9.1 Phase 1 — must-have

| Item | Definition of done |
|---|---|
| `AgentConnection` + `Session` refactor | Existing `_user_message_loop` tests pass with the new types. Main-agent UX unchanged. |
| `Session.prompt_awaitable(text) -> str` | Returns collected `AgentMessageChunk` text on `stop_reason=end_turn`. Per-session future tracked. |
| Per-session `allowed_tools` enforcement | Subagent attempting an out-of-allowlist tool gets denied via `request_permission`, verified by test. |
| `SubagentRegistry`, `SubagentRun`, ID generation | Unit-tested registry: open / get / close / list / collision retry. |
| Six MCP tools: `delegate`, `subagent_reply`, `subagent_status`, `subagent_wait`, `subagent_cancel`, `subagent_close`, `subagent_list` | Each tool's `{result}` shape matches §5. |
| `user_id` mapping via FastMCP session-id table | Verified by integration test (or stubbed at first). |
| Main bootstrap §"Delegation" block | Rendered into the bootstrap prompt; the §8.1 text. |
| Eviction wiring | When the main handle is evicted, cancel + close every subagent for that user. |

End of Phase 1: the main agent can `delegate`, peek, reply, wait, cancel, close. No `wait_all`, no `transcript`, no idle timeout.

### 9.2 Phase 2 — quality of life

| Item | Definition of done |
|---|---|
| `subagent_wait_all` MCP tool | Returns a batch result for every in-flight run. |
| `subagent_transcript` MCP tool | Returns full conversation history. |
| Idle timeout + background sweeper | Default 600 s; configurable via `ServeConfig.subagent_idle_timeout_s`. |
| Subagent `on_tool_action` → Telegram with `[<subagent_id>]` prefix | The user sees `[scout-a8f3] 🔧 find_placement(...)` in chat. |
| Per-call timeout for `delegate` and `subagent_reply` | Default 60 s; configurable. Surfaces as `status: "errored", last_error: "timeout"`. |
| Subagent status changes surface to Telegram | One concise line per state transition (`scout-a8f3 completed`, `wirer-d4e1 errored: ...`). |
| Soft `/cancel` semantics | `/cancel` cancels every in-flight turn (main + subagents) via `Session.cancel()` without evicting the `AgentConnection` or closing subagent sessions. Required for the §1.3 "persistent across multiple main-agent turns" promise. Today's behavior tears the connection down — see §6 table row. |

### 9.3 Phase 3 — deferred

- Per-subagent model selection (smaller model for `auditor`, smarter for `wirer`).
- Cross-user subagent sharing.
- Warm-pool of pre-spawned `claude-agent-acp` processes (only useful if Phase 1's process-startup cost turns out to matter; with one process per user it likely won't).
- Telegram `/subagents` command — list active subagents and recent activity.
- A `subagent_pause(subagent_id)` operation that freezes context without closing the session.

---

## 10. Open questions for revision

These are deliberately left open until implementation begins. Each ships with a recommendation.

1. **Subagent bootstrap exact text.** Sketched in §8.2 — needs polish once the first subagent runs end-to-end.
2. **Default per-call timeout.** Phase 2 introduces this. 60 s is a guess; revisit after observing real subagent runs.
3. **Are tool actions inside a subagent visible in the Telegram chat by default?** Phase 2 prefixes them with `[<subagent_id>]`. Could be made silent with a flag. Default visible, but the main agent's prompt should warn it about the noise.
4. **Per-subagent model override.** Phase 3. Spec field `AgentSpec.model: str | None` exists today but is unused for subagents; wiring through ACP `set_session_model` after `new_session` is straightforward when wanted.
5. **Subagent crash recovery.** Phase 1 records `errored` and leaves the run in the registry. Phase 3 could add automatic `subagent_revive` that opens a fresh session with the same `spec` and replays the transcript as priming.
6. **Authentication of subagent sessions to the MCP server.** Phase 1 inherits the MCP server's existing trust model — it's local loopback only. If the MCP server ever opens to network, subagents need a way to authenticate that ties them to the originating user.

---

## 11. Implementation roadmap (developer-facing)

Files to touch, in order of when they land:

```
Phase 1:
  python/src/timberbot/connector/
    session.py                    REFACTOR  split into AgentConnection + Session
    subagent.py                   NEW       SubagentRun, SubagentRegistry, ID generation
    agent_spec.py                 EDIT      add render_subagent_bootstrap()
  python/src/timberbot/game_mcp/
    server.py                     EDIT      add 7 delegate-family tools; accept SubagentRegistry factory
  python/src/timberbot/user_api/
    serve.py                      EDIT      hold {user_id → AgentConnection, SubagentRegistry};
                                            register MCP session-id ↔ user_id mapping
  python/tests/
    test_acp_connector.py         EDIT      split tests across new types
    test_subagent_registry.py     NEW       registry lifecycle, ID collision, eviction
    test_delegate_mcp.py          NEW       end-to-end via FakeAgent

Phase 2:
  python/src/timberbot/connector/subagent.py   EDIT  idle-sweeper task; timeout per call
  python/src/timberbot/game_mcp/server.py      EDIT  add wait_all + transcript
  python/src/timberbot/user_api/telegram/bot.py EDIT subagent ToolAction routing with prefix
```

No mod-side changes are required for either phase.

---

## 12. Open architectural risks

- **Anthropic-model behavior under multi-tool turns.** The premise of `wait=False` + `wait_all` is that the model emits multiple tool calls in one assistant turn. If a particular Claude model serializes tool calls within a turn, the parallelism collapses back to sequential — still correct, just no faster. Worth measuring once Phase 1 is up.
- **OpenCode parity.** All of this lands on `claude-agent-acp` first. OpenCode's `opencode acp` is the other supported backend; it should work identically (ACP is the abstraction) but the model's behavior on multi-tool emission may differ.
- **`subagent_wait_all` semantics under no in-flight runs.** Returns immediately with the current state of everything. Documented; not a bug, but worth covering in tests so the agent doesn't get confused.

---

## 13. Glossary

| Term | Meaning |
|---|---|
| AgentConnection | Wrapper around one `claude-agent-acp` (or `opencode acp`) subprocess plus its ACP stdio connection. Holds many sessions. |
| Session | One conversation thread inside an `AgentConnection`. Has its own session_id, allowed_tools, callbacks, dedup state. |
| AgentSpec | Code-level definition of an agent persona — slug, identity, tool scope, refusal text, behavior rules. |
| SubagentRun | Live state of one delegation: which spec, which session, status, transcript, last activity. |
| SubagentRegistry | Per-user dict of live SubagentRuns plus the idle-sweeper task. |
| Main session | The ACP session driving the Telegram chat (`Session.session_id == _acp_sessions[user_id]`). |
| Subagent session | Any other session on the same connection, opened via `delegate`. |
| Bootstrap prompt | The leading text block prepended to the first prompt of any new session, encoding identity + tool scope. |
