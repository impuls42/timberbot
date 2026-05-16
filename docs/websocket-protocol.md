# Timberbot WebSocket protocol

> Authoritative contract for the long-lived connector channel introduced in
> the v0.9 architecture rework (issue #27, foundation work in issue #28).
> The `openapi.yaml` document covers HTTP routes only; WebSocket framing is
> defined here.

## Upgrade

- URL: `ws://<host>:<wsPort>/api/ws`
- Default `wsPort`: `8086` (set in `settings.json`, alongside `httpPort`)
- Toggle: `wsEnabled` in `settings.json` (default `true`); when off the WS
  port is not opened and clients fall back to HTTP polling of
  `/api/agent/state`.

The HTTP server on `httpPort` is unchanged; the WS server is a separate
`HttpListener` on `wsPort`. Both ports share `listenAddress` and the
refuse-to-start guard: if `listenAddress` is non-loopback and `authToken`
is empty, neither listener starts.

## Authentication

When `authToken` is configured in `settings.json`, every WebSocket upgrade
MUST present the token via one of:

1. `Authorization: Bearer <token>` header on the HTTP/1.1 upgrade request.
2. `?token=<value>` query parameter on the upgrade URL (browser fallback
   for environments where setting custom headers on a WS upgrade is not
   supported).

Missing or invalid tokens get HTTP `401 Unauthorized` before the upgrade
completes. The comparison runs via `CryptographicOperations.FixedTimeEquals`
to avoid timing side-channels.

When `authToken` is empty, the WS channel is unauthenticated — only safe
for loopback binds. The refuse-to-start guard prevents accidentally
shipping an unauthenticated server on a non-loopback interface.

## Frame envelope

Every WebSocket frame is a UTF-8 text JSON object with exactly two keys:

```json
{
  "type": "<name>",
  "payload": { ... }
}
```

- `type` (string, required) selects the dispatch. Server matches
  case-insensitively (`PING` and `ping` are equivalent).
- `payload` (object, required for outbound frames; clients MAY omit it for
  parameterless inbound frames like `ping`). The shape depends on `type`.

Unknown `type` values produce a server-side `error` frame (see below) and
do not close the socket.

## Server → client frames

### `state`

Sent on every change to `TimberbotAgentState`. Subscribers should treat
each `state` frame as the authoritative snapshot — no diffing is required.

```json
{
  "type": "state",
  "payload": {
    "mode": "request",
    "goal": "two settlements survive winter",
    "ready": true,
    "agentStatus": "idle",
    "lastError": null,
    "pendingRequest": {
      "id": 12,
      "prompt": "place a Lumberjack near the river"
    }
  }
}
```

The `payload` shape mirrors the `AgentState` schema in `openapi.yaml`. A
fresh `state` frame is pushed immediately after a successful upgrade so
clients don't need to issue a separate `GET /api/agent/state`.

### `event`

Game-event broadcast (replaces the deleted outbound HTTP webhook fan-out).
Subscribers MAY filter on `payload.event` client-side; the server does not
support subscription filtering.

```json
{
  "type": "event",
  "payload": {
    "event": "day.start",
    "day": 7,
    "timestamp": 1700000000,
    "data": { "day": 7 }
  }
}
```

- `event` (string): event name (e.g. `day.start`, `drought.start`,
  `building.finished`, `beaver.born`). See
  `timberbot/src/TimberbotEvents.cs` for the canonical event list.
- `day` (integer): the in-game day number at emission.
- `timestamp` (integer): Unix seconds at emission.
- `data` (object | null): event-specific payload. Events without
  per-event data ship `null` here.

### `error`

Server-side error after parsing or dispatching an inbound frame. Never
fatal — the socket stays open.

```json
{"type": "error", "payload": {"error": "unknown_type: foo"}}
```

### `pong`

Reply to a client `ping`. Empty payload.

```json
{"type": "pong", "payload": {}}
```

## Client → server frames

### `heartbeat`

Sent on a 30-second cadence by the connector to advertise liveness and
acknowledge requests it has handled.

```json
{
  "type": "heartbeat",
  "payload": {
    "version": "0.9.0",
    "agent_status": "running",
    "acked_request_id": 12
  }
}
```

- `version` (string): client/connector version. Optional today; reserved
  for forward-compat protocol negotiation.
- `agent_status` (string): free-form connector-reported status. Empty
  string = no status update (does not clear the field).
- `acked_request_id` (integer ≥ 0): highest `pendingRequest.id` the
  connector has handled. When `>= pendingRequest.id`, the mod clears the
  pending slot and broadcasts a `state` frame reflecting the cleared slot.

A heartbeat always produces a `state` frame as the response — the
connector can use that round-trip as a TCP keepalive check.

### `ping`

Liveness probe. Server replies with a `pong` frame. Use this when you need
a tighter cadence than the 30-second heartbeat.

```json
{"type": "ping", "payload": {}}
```

## Liveness & timeouts

- Application-level heartbeat: 30 seconds. The server records the last
  heartbeat timestamp on `TimberbotAgentState` for observability, but
  connection liveness itself is driven by the TCP keepalive and the WS
  ping/pong machinery.
- TCP keepalive: enabled at the OS layer for the listener; clients should
  rely on `WebSocket.State` transitions for hard-disconnect detection.
- Per-connection bounded send queue (256 frames by default). Slow consumers
  whose queue fills are dropped — the connection is closed with reason
  `slow_consumer` and a fresh `state` frame is delivered when they
  reconnect. State-broadcast fan-out NEVER stalls on a slow consumer.

## Reconnect & backoff

When the WS connection drops, clients should:

1. Reconnect with exponential backoff: start at 1 second, double up to
   30 seconds, jitter by ±20%.
2. Treat the initial `state` frame after reconnect as the authoritative
   snapshot — discard any cached pre-disconnect state.
3. Re-emit the latest `acked_request_id` in the next `heartbeat` so the
   mod can re-evaluate the pending slot.

There is no per-session resume token; the channel is intentionally
stateless on the client side.

## Versioning

The frame envelope (`{type, payload}`) and the message types listed above
are part of the `OPENAPI_VERSION` contract (`1.0.0` at time of writing).
Bump the major version when removing or renaming a message type. Adding
new message types or new payload fields is a minor / patch change.

Clients SHOULD treat unknown `type` values as a no-op (ignore) and unknown
`payload` fields as forward-compatibility hints.
