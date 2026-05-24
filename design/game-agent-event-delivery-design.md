# Game Agent Event Delivery — Tool Result Augmentation

| Field | Value |
|---|---|
| Status | Design proposal — partial implementation in `tbot serve` MCP envelope (see [`../docs/getting-started.md`](../docs/getting-started.md) §"Talk to the agent over Telegram"). Source of truth for live behavior: `python/src/timberbot/game_mcp/`. |
| Version | 0.1 |
| Scope | Event delivery mechanism between game runtime and gaming agent |
| Out of scope | ACP transport, MCP server bootstrap, agent runtime selection, game-side event generation |

---

## 1. Context

### 1.1 System architecture (brief)

The gaming agent platform is a three-layer system:

- **Game connector** — orchestrator; exposes a user-facing API, manages session lifecycle, hosts the game-side MCP server, acts as the ACP client toward the agent runtime, owns the safety stop.
- **Agent runtime** — any ACP-compatible runtime (Claude Code, OpenCode, Gemini CLI, Codex, etc.) spawned per session as a subprocess of the connector. Swappable.
- **Game MCP server** — exposes typed game capabilities (actions, queries, knowledge) to the agent runtime. Conventionally co-located with the connector or invoked in-process.

The agent runtime talks to the connector over ACP (JSON-RPC over stdio). The agent runtime talks to the game MCP server over MCP (configured per-session via ACP `session/new`). The connector and the game MCP server share state — most importantly, the event log this document describes.

### 1.2 The constraint

The agent runtime executes a turn-based loop: receive prompt → LLM generates with optional tool calls → return response. There is no protocol-level mechanism in MCP or ACP to inject information into an in-flight LLM generation. This is a hard constraint shared across all current runtimes outside the realtime-model families (OpenAI Realtime, Gemini Live).

For a gaming agent, this means the game world can change between the moment the agent issued a tool call and the moment it next acts. Without a delivery mechanism, those changes are invisible to the agent until it explicitly queries state, and a naïve `read_state` polling loop is both expensive (tokens per poll) and slow to react.

### 1.3 Decision

Every game-side tool call returns its result wrapped in a standardized envelope that includes all world events accumulated since the agent's previous tool call. The agent observes world changes as a natural side-effect of acting, with one explicit `observe()` tool available when it needs to check without taking an action.

This document specifies the envelope, the cursor mechanics, the event log, the configuration surface, the agent guidance, and the failure modes.

### 1.4 What this is not

- Not a real-time streaming mechanism. The agent observes events at tool-call cadence, not as they happen.
- Not a substitute for `session/cancel`. The advisory field is a soft signal; hard stops use ACP cancellation.
- Not a replacement for MCP resource subscriptions. Resource subscriptions remain useful for forward compatibility but are not the primary delivery channel.

---

## 2. High-level design

```
Game runtime ──events──▶ Connector EventBus ──┐
                                              ▼
                              ┌─────────────────────────┐
                              │  Per-session event log  │
                              │  (ring buffer + seq)    │
                              └────────────┬────────────┘
                                           │ drain since cursor
Agent ──tool call──▶ MCP server ───────────┤
                          │                ▼
                          │     build envelope,
                          │     advance cursor
                          ▼
Agent ◀──tool envelope────┘
        (result + events_since)
```

### 2.1 Components

| Component | Responsibility |
|---|---|
| `EventBus` | Connector-side ingestion API. Game runtime calls this to publish events. |
| `EventLog` | Per-session ring buffer of `GameEvent` records, indexed by monotonic `seq`. |
| `CursorStore` | Per-session record of `(consumed, request_id_cache)`. Atomic with envelope construction. |
| `SubscriptionFilter` | Per-session config: severity floor, type globs, max events per envelope. |
| `MCP server tool handlers` | Each game tool delegates envelope construction to a shared `build_envelope` function after performing its side-effect. |
| `Compactor` | Background and on-demand task that folds collapsible events and produces summary events on eviction. |

### 2.2 Lifecycle

1. Connector spawns agent runtime; passes game MCP server config via ACP `session/new`.
2. Connector instantiates per-session `EventLog`, `CursorStore`, `SubscriptionFilter`.
3. Game runtime begins emitting events through `EventBus.emit(sessionId, event)`.
4. Agent issues tool calls. Each handler calls `build_envelope` after its side-effect.
5. Envelope is returned over MCP. Agent observes events as part of the natural response.
6. On `session/cancel` or session end, per-session state is released.

---

## 3. Data model

### 3.1 GameEvent

```typescript
interface GameEvent {
  seq: number;           // monotonic per session, gap-free, assigned by EventBus
  ts: string;            // ISO 8601, connector clock
  type: string;          // namespaced: "world.weather", "actor.player.entered"
  severity: Severity;
  payload: unknown;      // type-specific, schema documented per type registry
  ttl_ms?: number;       // optional; default never expires
  collapsible?: string;  // events sharing a key may be folded during compaction
}

type Severity = "trace" | "info" | "notice" | "warn" | "critical";
```

### 3.2 Severity taxonomy

The connector and game runtime share this taxonomy. Severity drives filtering, eviction priority, and advisory escalation.

| Severity | Meaning | Examples |
|---|---|---|
| `trace` | Diagnostic, normally filtered out | actor-tick state, internal scheduler events |
| `info` | Routine world activity | background actor movement, ambient changes |
| `notice` | Notable but not urgent | player entered named area, item discovered |
| `warn` | Requires agent attention | hostile spawned, resource threshold crossed |
| `critical` | Demands immediate response | player damaged, mission constraint violated |

A game design document should map every event type to a fixed severity. Severity should be a property of the event type, not chosen per emission, to keep the agent's mental model stable.

### 3.3 Type namespacing

Types follow `domain.entity.action` form:

- `world.*` — environmental, non-actor events
- `actor.<role>.*` — actor-level events (`actor.player.*`, `actor.npc.*`, `actor.background.*`)
- `system.*` — connector or MCP internal events (e.g. `system.compacted`, `system.halt`)
- `game.<feature>.*` — game-specific feature domains

The `system.*` namespace is reserved for the connector. Game runtimes must not emit `system.*` events.

### 3.4 Collapsible key semantics

When `collapsible` is set on an event, the compactor may fold multiple events sharing the same `(type, collapsible)` pair into a single summary event during eviction or on-demand compaction. Use cases:

- A patrol of background actors emits one `actor.background.moved` event per tick → all collapse under `collapsible: "patrol-7"`.
- Weather oscillates between states → events share `collapsible: "weather-cycle"`.

Events without a `collapsible` key are not folded.

---

## 4. Tool envelope

### 4.1 Schema

Every game-side MCP tool returns:

```typescript
interface ToolEnvelope<TResult> {
  result: TResult;            // tool-specific success payload
  meta: {
    cursor: {
      consumed: number;       // last seq the agent has now seen
      high_water: number;     // current connector seq at envelope build time
    };
    events: GameEvent[];      // events in (previous_consumed, high_water]
    events_truncated: boolean;
    events_dropped: number;   // count lost to ring-buffer eviction since last call
    advisory: Advisory;
    hint?: string;            // optional natural-language suggestion
  };
}

type Advisory = "normal" | "attention" | "urgent" | "halt";
```

### 4.2 Consistency requirement

Every tool exposed to the agent — including `observe()`, knowledge queries, state reads — returns this envelope. The agent learns one pattern; the connector emits one shape. Tool result schemas vary in `result`; the `meta` block is invariant.

### 4.3 Advisory semantics

| Advisory | Meaning | Expected agent behavior |
|---|---|---|
| `normal` | No elevated attention required | Continue planned action |
| `attention` | Notable events present; re-evaluate before next action | Read events; consider plan adjustment |
| `urgent` | Critical events present; re-plan around them | Stop current plan; address events |
| `halt` | Connector requests graceful shutdown | Acknowledge with no-op tool call; expect session/cancel next |

`halt` is special and discussed in §8.

### 4.4 Hint field

`hint` is an optional natural-language string the connector can include to nudge agent behavior. Examples: `"weather is changing, consider shelter"`, `"player has been idle 60s"`. Use sparingly — agent guidance should primarily come from event content and advisory level, not from connector-side nudges.

---

## 5. Cursor and idempotency

### 5.1 Cursor model

Per-session cursor: `{ consumed: number, request_id_cache: Map<RequestId, EnvelopeSnapshot> }`.

The cursor lives in the connector. The agent does not track it independently. After every successful envelope construction, `consumed` advances to the `high_water` value used in that envelope.

### 5.2 Tool call sequence

```
1. Tool handler receives call with request_id from MCP
2. Check request_id_cache; if hit, return cached envelope (idempotency)
3. Execute tool side-effect (or read)
4. Read high_water = EventLog.high_water(sessionId)
5. Drain events from (cursor.consumed, high_water] applying SubscriptionFilter
6. Compute advisory from drained events
7. Build envelope
8. ATOMICALLY:
     - cursor.consumed = high_water
     - request_id_cache[request_id] = envelope_snapshot
9. Return envelope
```

Steps 4–8 must observe linearizable ordering with respect to other tool calls on the same session. In practice this means a per-session mutex around envelope construction.

### 5.3 Idempotency cache

`request_id_cache` is bounded (e.g., last 64 requests) and used to deduplicate retries. If a transport-level retry occurs, the cached envelope replays without advancing the cursor again. Entries expire on session close.

### 5.4 Replay semantics

The agent may explicitly request replay via `observe({ since: <seq>, advance_cursor: false })`. This is useful for:

- Runtime restart with `session/load` — agent fetches events from last seq seen in conversation history.
- Context recovery after compaction — agent inspects a range it previously consumed.

Replays do not modify the cursor when `advance_cursor: false`.

---

## 6. Buffer management

### 6.1 Sizing

Default ring buffer size: **256 events per session**. Configurable via `game_events.buffer_size`. Sizing should accommodate the expected event rate over the longest expected agent thinking time (e.g., if the agent may take 30s between calls and the game emits 5 events/sec, allocate at least 150 + safety margin).

### 6.2 Eviction policy

Eviction is severity-weighted, not strict FIFO. When the buffer reaches capacity:

1. Among events older than the current consumed cursor, evict in severity order: `trace` first, then `info`, `notice`, `warn`. `critical` events resist eviction until last.
2. If no consumed-and-evictable events exist, evict from unconsumed events in the same severity order. **`critical` events are never evicted unconsumed** — instead, the buffer raises a `system.buffer_overflow` event and forces compaction.

### 6.3 Compaction

When eviction occurs, a synthetic event is inserted at the buffer tail:

```json
{
  "seq": 13420,
  "ts": "2026-...",
  "type": "system.compacted",
  "severity": "notice",
  "payload": {
    "range": [13104, 13380],
    "summary": "12 actor.moved (patrol-7), 3 world.weather transitions",
    "by_type": {
      "actor.moved": 12,
      "world.weather": 3
    },
    "evicted_critical": 0
  }
}
```

This event takes the place of the evicted range in the agent's perception. The compactor folds collapsible events first (cheap), then summarizes by type (lossy but bounded).

A more sophisticated compactor may invoke an LLM via MCP sampling to produce a natural-language digest. This is an opt-in upgrade path; default is type-count aggregation.

### 6.4 TTL

Events with `ttl_ms` set are removed by a lazy reaper that runs on every `drain()` operation. Expired events do not appear in envelopes. TTL is independent of consumed state — expired events are dropped regardless of whether the agent has seen them.

---

## 7. The observe() tool

### 7.1 Signature

```typescript
observe({
  since?: number,           // default: cursor.consumed
  until?: number,           // default: high_water
  limit?: number,           // default: 32, hard cap: 128
  types?: string[],         // glob patterns; default: subscription filter
  min_severity?: Severity,  // default: subscription filter
  advance_cursor?: boolean  // default: true
}) → ToolEnvelope<{}>
```

### 7.2 Use cases

| Use case | Invocation |
|---|---|
| "What changed while I was thinking?" | `observe()` |
| "Peek without committing" | `observe({ advance_cursor: false })` |
| "Replay recent critical events" | `observe({ since: N, min_severity: "critical", advance_cursor: false })` |
| "Filter to player events" | `observe({ types: ["actor.player.*"] })` |

### 7.3 No side effects

`observe()` produces no game-side state change. `result` is always `{}`. The envelope's `meta` block carries all information.

### 7.4 Cursor advance default

`advance_cursor: true` is the default because the typical use of `observe()` is the agent checking what happened. Explicit `false` exists for peek and replay.

---

## 8. Advisory escalation and halt

### 8.1 Mapping

```
highest severity in envelope.events    → advisory
-----------------------------------    --------
only trace/info                        → normal
includes notice                        → normal
includes warn                          → attention
includes critical                      → urgent
includes system.halt                   → halt
```

Advisory is computed per envelope. An envelope with no events has `advisory: normal`.

### 8.2 The halt signal

`halt` is emitted by the connector via `EventBus` when:

- User-initiated stop (safety feature from original requirements)
- Game-state-driven graceful end (mission complete, session timeout)
- Operator-initiated stop (admin/connector control plane)

When `halt` fires:

1. A `system.halt` event is published with the stop reason
2. The next envelope to the agent carries `advisory: halt`
3. The agent has **one tool call** to acknowledge — a no-op like `observe()` or a final state report
4. If the agent does not acknowledge within a timeout (default 10s), the connector escalates to ACP `session/cancel`
5. If the agent acknowledges, the connector still calls `session/cancel` after the response, but the agent had a graceful exit window

This gives well-behaved agents a clean shutdown path while preserving the hard-stop guarantee via ACP cancellation.

### 8.3 Graceful vs hard stop

| Path | Mechanism | When to use |
|---|---|---|
| Graceful | `halt` advisory + agent ack + `session/cancel` | Normal end-of-session, user-requested stop |
| Hard | Immediate `session/cancel` (skip halt) | Safety violation, agent malfunction, force-quit |

The connector decides which path based on the stop trigger.

---

## 9. Configuration

### 9.1 Per-session config

Passed at MCP server initialization via `mcpServers` config in ACP `session/new`:

```json
{
  "game_events": {
    "max_per_envelope": 16,
    "min_severity": "info",
    "subscribe": ["world.*", "actor.*", "!actor.background.*"],
    "ttl_default_ms": null,
    "buffer_size": 256,
    "compaction": "summarize_on_evict",
    "advisory_thresholds": {
      "attention_above": "notice",
      "urgent_above": "warn"
    },
    "halt_ack_timeout_ms": 10000
  }
}
```

### 9.2 Defaults rationale

- `max_per_envelope: 16` — keeps token cost bounded; agent rarely needs more per turn
- `min_severity: "info"` — trace events filtered out unless debugging
- `subscribe` glob with `!` exclusion — game-specific; default subscribes to player and world but not background actors
- `buffer_size: 256` — accommodates ~50 seconds of think-time at 5 events/sec
- `compaction: "summarize_on_evict"` — count-based summarization, no LLM dependency
- `halt_ack_timeout_ms: 10000` — long enough for the agent's current turn to wrap

### 9.3 Per-role config variants

Different agent roles in the same game may want different subscriptions. Suggested presets:

- **Commander** — subscribe to high-level world + player events; aggressive filter on background activity
- **Scout** — subscribe to actor and world events broadly; higher buffer size for fast-evolving situations
- **Narrator** — subscribe to player events and lore-relevant changes; longer TTL, lower severity floor

Presets ship as named configurations; sessions select by role.

---

## 10. Connector-side ingestion

### 10.1 EventBus API

Internal to the connector, not exposed to the agent:

```typescript
interface EventBus {
  emit(sessionId: string, event: EventInput): number
  emitBroadcast(event: EventInput): void
  subscribe(sessionId: string, filter: SubscriptionFilter): Subscription
}

interface EventInput {
  type: string
  severity: Severity
  payload: unknown
  ttl_ms?: number
  collapsible?: string
}
```

`emit` returns the assigned `seq` so the game runtime can correlate emissions with downstream effects for telemetry.

### 10.2 Routing

- `emit(sessionId, event)` — single-session target
- `emitBroadcast(event)` — fan-out to all active sessions, subject to per-session subscription filter

For games with multiple agent sessions (e.g., separate NPCs each driven by a separate agent), routing rules must be explicit in the game-side code.

### 10.3 Backpressure

The bus does not block emitters. If a session's buffer is full, eviction happens at write time. Game-side code should not be aware of buffer pressure.

---

## 11. Agent guidance

### 11.1 System prompt fragment

Delivered at session start by the connector or as an MCP prompt template:

> Every tool you call returns a `meta` object containing world events that have occurred since your last action. Scan `meta.events` before deciding your next move.
>
> The `meta.advisory` field summarizes the urgency of those events:
> - `normal` — proceed with your current plan
> - `attention` — re-evaluate before acting
> - `urgent` — stop your current plan and address the events
> - `halt` — the session is ending; respond with a brief acknowledgment
>
> If you suspect the world has changed but have no action to take, call `observe()` to check.

### 11.2 MCP prompt template

A reusable prompt named `game.session_intro` carries this guidance. The connector includes it in the first prompt of every session.

### 11.3 Reinforcement events

If the agent appears to ignore `urgent` advisories (e.g., emits a routine action when critical events were just delivered), the connector may emit a `system.attention_reminder` event in the next envelope. This is a behavioral nudge, not a guarantee.

---

## 12. Failure modes

| Failure | Mitigation |
|---|---|
| Event flood from game | Severity floor on subscription; bounded buffer; compaction on overflow |
| Agent ignores events | Events in every response (hard to miss); `advisory` field elevates priority; system prompt instructs scanning; `urgent` advisory + reminder events |
| Cursor drift across runtime restart | Cursor lives connector-side; on `session/load`, agent uses `observe({ since: <last seen in history> })` |
| Tool call retry | `request_id_cache` makes envelope construction idempotent |
| Cross-session event leakage | Subscription filters are session-scoped; `EventLog` instances are per-session |
| Stale events poisoning context | `ttl_ms` reaping on every drain; compaction summaries replace originals |
| Critical event lost to eviction | Critical events never evicted unconsumed; `system.buffer_overflow` raised if buffer fills with critical events |
| Game-side `system.*` emission | Connector validates type namespace at `emit`; rejects with logged error |
| Agent ignores `halt` | Timeout escalates to `session/cancel` |

---

## 13. Implementation plan

### 13.1 Phase 1 — Minimum viable envelope

**Goal:** End-to-end envelope working with one game tool and `observe()`.

- `GameEvent` schema, `EventLog` ring buffer (no eviction), `CursorStore` with idempotency cache
- `EventBus.emit` and `emitBroadcast`
- `build_envelope` helper
- One representative game tool wrapping `build_envelope`
- `observe()` tool
- Hard-coded subscription config

**Exit criteria:** Agent receives events on every tool call; cursor advances correctly; retries are idempotent.

### 13.2 Phase 2 — Severity, filtering, compaction

- Severity-weighted eviction
- `SubscriptionFilter` with glob and severity floor
- Count-based compaction with `system.compacted` events
- TTL reaper

**Exit criteria:** Buffer survives a 10x event flood test; envelopes stay within `max_per_envelope`; no critical event lost.

### 13.3 Phase 3 — Configuration and per-session tuning

- Per-session `game_events` config via ACP `session/new`
- Role-based config presets
- `halt` advisory and graceful shutdown path
- `system.attention_reminder` behavioral nudge

**Exit criteria:** Multiple concurrent sessions with different configs run without cross-contamination; halt path completes within `halt_ack_timeout_ms` for well-behaved agents.

### 13.4 Phase 4 — Observability and ops

- Per-session metrics: events emitted, delivered, dropped, compacted; envelope size distribution; advisory distribution
- Trace logging of envelope construction with sampling
- Dashboard or CLI for inspecting live session state
- Optional: LLM-summarized compaction via MCP sampling

**Exit criteria:** Operator can diagnose a "agent ignored an event" report from logs alone; tuning recommendations can be made from metrics.

---

## 14. Open questions

1. **Parallel tool calls.** Newer agent runtimes can issue multiple tool calls in parallel. Per-session mutex serializes envelope construction, but should each parallel call see the same `high_water`, or should they observe linearizable advance? Recommend: each call sees its own high-water snapshot at start; cursor advances to the latest of the parallel batch. Needs validation against actual runtime behavior.

2. **LLM-summarized compaction.** Phase 4 mentions this as opt-in. Cost vs. fidelity trade-off needs measurement on representative event streams.

3. **Persistence.** Should `EventLog` survive connector restart? For long-running sessions, losing the log breaks `session/load` recovery. Recommend: optional disk-backed log with replay on restore; off by default.

4. **Event schema registry.** Game-specific event types and their payload schemas should be documented somewhere agents (and humans) can discover. Recommend: an MCP resource `game://event-schemas/{type}` returning JSON schema; out of scope for this doc but adjacent.

5. **Cross-session correlation.** Some events relate to multiple sessions (e.g., player A and agent B in the same world). Routing today fans these out per-session, but cross-session correlation IDs may be needed for analytics. Defer until a concrete use case lands.

6. **Pre-emption inside long tools.** If a game tool itself takes 30s and during that time a `critical` event arrives, the agent doesn't see it until the tool returns. MCP Tasks (SEP-1686) plus elicitation may let us interrupt long tools to surface events. Out of scope for v1.

---

## 15. References

### Protocol specifications

- Model Context Protocol — https://modelcontextprotocol.io/specification
- Agent Client Protocol — https://agentclientprotocol.com
- MCP SEP-1686 Tasks — async task model
- MCP resource subscriptions — `notifications/resources/updated`

### Related architectural decisions

- ADR-001 (assumed): Adoption of ACP for swappable agent runtimes
- ADR-002 (assumed): Game MCP server as the typed game-capability surface
- ADR-003 (assumed): Game connector as orchestrator and ACP client

### Background reading

- ReAct loop (Yao et al., 2022) — the agent observe-think-act cycle this design assumes
- Voice agent barge-in patterns — analogous cancel-and-restart approach for true mid-turn interruption when needed

---

## Appendix A — Pseudocode

### A.1 Envelope construction

```python
async def build_envelope(session_id, result, request_id):
    async with session_mutex[session_id]:
        cached = idempotency_cache.get(session_id, request_id)
        if cached:
            return cached

        cursor = cursor_store.get(session_id)
        high_water = event_log.high_water(session_id)
        config = subscription_config[session_id]

        events, truncated, dropped = event_log.drain(
            session_id,
            since=cursor.consumed,
            until=high_water,
            filter=config.subscription,
            limit=config.max_per_envelope,
        )

        advisory = compute_advisory(events, config)

        envelope = {
            "result": result,
            "meta": {
                "cursor": {"consumed": high_water, "high_water": high_water},
                "events": events,
                "events_truncated": truncated,
                "events_dropped": dropped,
                "advisory": advisory,
            },
        }

        cursor_store.set(session_id, consumed=high_water)
        idempotency_cache.put(session_id, request_id, envelope)
        return envelope
```

### A.2 Example tool

```python
@mcp.tool()
async def game_move(direction: str, ctx: Context) -> dict:
    session_id = ctx.session_id
    request_id = ctx.request_id
    result = await game_runtime.execute_move(session_id, direction)
    return await build_envelope(session_id, result, request_id)
```

### A.3 The observe tool

```python
@mcp.tool()
async def observe(
    ctx: Context,
    since: int | None = None,
    until: int | None = None,
    limit: int | None = None,
    types: list[str] | None = None,
    min_severity: str | None = None,
    advance_cursor: bool = True,
) -> dict:
    # Parametrized variant of build_envelope; details elided.
    ...
```

---

*End of document.*
