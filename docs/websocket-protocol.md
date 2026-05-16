# WebSocket protocol

> **Placeholder.** The authoritative wire contract ships with [#28](https://github.com/impuls42/timberbot/issues/28) (WS Unit 1: C# server foundation). This page is a stub so the v0.9 docs cross-references resolve while the units land in parallel. Once Unit 1 merges, this file is rewritten with the full contract — envelope, message types, auth, reconnect guidance, JSON examples.

## Summary (until Unit 1 lands)

- Upgrade URL: `ws://host:wsPort/api/ws` (default `wsPort` is `8086`)
- Auth: `Authorization: Bearer <token>` on the upgrade request when the mod's `authToken` is set, or `?token=<token>` as a query-param fallback
- Frame envelope: `{"type": "<name>", "payload": {...}}` JSON
- Server→client message types: `state`, `event`, `error`, `pong`
- Client→server message types: `heartbeat` (carries `version`, `agent_status`, `acked_request_id`), `ping`
- Heartbeat cadence: 30 s. WS ping/pong and TCP keepalive handle liveness.
- Reconnect: client responsibility. Exponential backoff (1 s → 30 s cap) is the project convention.

For the consumer-facing guide, see [events.md](events.md). For the mod-side architecture, see [architecture.md](architecture.md#websocket-protocol).
