# Events

> **v0.9 — WebSocket cutover, in flight.** The mod no longer dispatches outbound HTTP webhooks. Game events are now pushed over the mod's WebSocket endpoint (a separate connection per subscriber, sharing the same endpoint the connector uses); see [`websocket-protocol.md`](websocket-protocol.md) for the wire contract.

Game events (drought, beaver deaths, building placement, weather, power, wonders, …) are delivered as server-push frames on the mod's WebSocket endpoint at `ws://host:wsPort/api/ws` (default port `8086`). Any number of subscribers can connect; each receives the same fan-out stream.

Events are **unaffected by the [ready gate](architecture.md#ready-gate)** — they keep firing whether the player has pressed Launch or not. That's deliberate: a game-event subscriber (a Discord bot, a dashboard, an alerting daemon) should still see what's happening in the colony even when the AI is muted.

## Quick start: `tbot listen`

`tbot listen` is a pure WebSocket client that connects to the mod, prints every event as it arrives, and exits cleanly on `Ctrl-C`. It exists so you can debug event delivery without writing any code.

```bash
tbot listen                          # one event per line as JSON
tbot listen --pretty                 # human-friendly rendering
tbot listen --forward-to events.log  # tee each event as one JSON line
tbot listen --forward-to https://collector.example/sink   # POST each event downstream
```

`--forward-to` now writes / POSTs one event object per line (or per request), not the JSON array the old HTTP webhook receiver produced. If you had a downstream consumer parsing the array shape, switch it to parse a single event per call.

`tbot listen` picks up `[client].host` and `[client].auth_token` from `~/.config/timberbot/config.toml` (the same auth token the HTTP client uses), and defaults the WS port to `8086`. Override per invocation:

```bash
tbot listen --host=192.168.1.50 --port=8086 --auth-token=s3cret
```

There is no `--listen-port` — `tbot listen` is a client, not a server. Nothing inbound to your machine.

## Frame shape

Every server→client frame uses the same envelope:

```json
{ "type": "event", "payload": { "event": "drought.start", "day": 45, "timestamp": 1711300000, "data": { "duration": 8 } } }
```

`payload` carries the event record. The fields are stable across all 68 event types:

| Field | Meaning |
|---|---|
| `event` | dot-separated event name (e.g. `drought.start`, `beaver.died`) |
| `day` | in-game day number when the event fired |
| `timestamp` | Unix epoch seconds when the mod published the frame |
| `data` | per-event payload (may be `null`) |

The other frame types (`state`, `error`, `pong`) are documented in [`websocket-protocol.md`](websocket-protocol.md). Server→client `event` frames are what this page is about.

## Writing your own subscriber

Any WebSocket client works. The Python helper that ships with the package is the easiest entry point:

```python
import asyncio
from timberbot.api.wsclient import TimberbotWebSocket

async def main():
    async with TimberbotWebSocket("ws://127.0.0.1:8086/api/ws", auth_token=None) as ws:
        async for frame in ws:
            if frame.type == "event":
                print(frame.payload["event"], frame.payload["data"])

asyncio.run(main())
```

Browser / non-Python clients connect to the same URL. Pass the bearer token via `Authorization: Bearer <token>` on the upgrade request, or via `?token=<token>` as a query-string fallback when the upgrade headers aren't reachable (browsers).

## Differences vs. the old HTTP POST model

If you previously hosted an HTTP server and registered it via `POST /api/tbot/register` or `POST /api/webhooks`, here's what changed:

| Old (deleted) | New |
|---|---|
| Mod POSTs to your URL | Mod pushes a frame on the open WS |
| Per-subscriber URL registry stored on the mod | No registration — any client that opens the WS receives the fan-out |
| Per-event filter in the registration payload | Filter client-side on `payload.event` |
| Circuit breaker auto-disables a flaky URL | Slow consumers are dropped from the bounded send queue; reconnect is the client's job |
| Batched POST body (JSON array) | One frame per event |
| Subscriber needed an inbound HTTP server | Subscribers are pure outbound clients |
| `POST /api/webhooks`, `POST /api/webhooks/delete`, `GET /api/webhooks`, `POST /api/tbot/register` | Deleted — open the WS instead |
| `webhooksEnabled`, `webhookBatchMs`, `webhookCircuitBreaker`, `webhookMaxPendingEvents`, `webhookValidateUrls` in `settings.json` | All removed — logged as ignored on load |
| `tbot register_webhook`, `tbot unregister_webhook`, `tbot list_webhooks` CLI commands | Deleted — use `tbot listen` |
| `--listen-port` on `tbot watch` / `tbot listen` | Deleted — no inbound server |

Operationally, this means:

- **No registration step.** Just connect.
- **No SSRF surface.** The mod never makes outbound HTTP calls for events.
- **No port exposed on the subscriber.** Subscribers run anywhere with outbound TCP to the mod.
- **Events that fire while disconnected are dropped.** WS reconnect is the client's responsibility. If you need durability, write a subscriber that batches into your own queue.

## Events catalog (68 total)

The event names and payload shapes are unchanged from the previous HTTP design. Filter on `payload.event` client-side.

### Weather (7)

| Event | Fires when |
|---|---|
| `drought.start` | drought/badtide begins |
| `drought.end` | drought/badtide ends |
| `drought.approaching` | drought warning (UI notification) |
| `weather.selected` | next weather type chosen for cycle |
| `cycle.start` | new weather cycle begins |
| `cycle.end` | weather cycle ends |
| `cycle.day` | new day within weather cycle |

### Time (2)

| Event | Fires when |
|---|---|
| `day.start` | dawn |
| `night.start` | dusk |

### Buildings (9)

| Event | Fires when |
|---|---|
| `building.placed` | building placed on map |
| `building.demolished` | building demolished |
| `building.finished` | construction complete |
| `building.unfinished` | building reverted to unfinished |
| `building.unlocked` | science unlock |
| `building.deconstructed` | building deconstructed |
| `construction.started` | construction begins |
| `demolish.marked` | marked for demolition |
| `demolish.unmarked` | demolition mark removed |

### Blocks (2)

| Event | Fires when |
|---|---|
| `block.set` | any block placed (paths, levees, platforms) |
| `block.unset` | any block removed |

### Population (8)

| Event | Fires when |
|---|---|
| `beaver.born` | beaver/bot created (from entity system) |
| `beaver.born.event` | beaver born (from beaver system) |
| `beaver.died` | beaver/bot died (from entity system) |
| `character.created` | character created |
| `character.killed` | character killed |
| `bot.manufactured` | bot assembled |
| `population.changed` | population count changed |
| `migration` | beaver migrated between districts |

### Districts (3)

| Event | Fires when |
|---|---|
| `district.changed` | district added/removed |
| `district.connections.changed` | path connections between districts changed |
| `migration.district.changed` | migration district selection changed |

### Needs/Wellbeing (5)

| Event | Fires when |
|---|---|
| `contamination.changed` | beaver contamination status changed |
| `teeth.chipped` | beaver teeth chipped (injury) |
| `wellbeing.highscore` | new wellbeing highscore |
| `status.alert` | status alert added |
| `status.dynamic.alert` | dynamic status alert added |

### Trees/Crops (8)

| Event | Fires when |
|---|---|
| `tree.cut` | tree cut down |
| `tree.marked` | tree added to cutting area |
| `cuttable.cut` | cuttable resource harvested |
| `cutting.area.changed` | cutting area modified |
| `crop.planted` | natural resource planted |
| `planting.marked` | planting area marked |
| `planting.coords.set` | specific planting tile set |
| `planting.coords.unset` | planting tile cleared |

### Wonders (3)

| Event | Fires when |
|---|---|
| `wonder.activated` | wonder activated |
| `wonder.completed` | wonder completed |
| `wonder.countdown` | wonder completion countdown started |

### Power (4)

| Event | Fires when |
|---|---|
| `power.network.created` | power network created |
| `power.network.removed` | power network destroyed |
| `power.generator.added` | generator added to network |
| `power.generator.updated` | generator output changed |

### Game State (8)

| Event | Fires when |
|---|---|
| `game.over` | all beavers dead |
| `game.new` | new game started |
| `game.starting.building` | first building placed in new game |
| `speed.changed` | game speed changed |
| `speed.lock.changed` | speed lock toggled |
| `workhours.changed` | work hours changed |
| `workhours.transitioned` | work hours transitioned |
| `autosave` | autosave triggered |

### Explosions (2)

| Event | Fires when |
|---|---|
| `explosion` | dynamite detonated |
| `explosion.kill` | beaver killed by explosion |

### Terrain/Wind (2)

| Event | Fires when |
|---|---|
| `terrain.destroyed` | terrain destroyed |
| `wind.changed` | wind direction/speed changed |

### Misc (5)

| Event | Fires when |
|---|---|
| `zipline.activated` | zipline connection activated |
| `entity.created` | any entity created (low-level) |
| `entity.renamed` | entity renamed |
| `construction.mode.changed` | entered/exited construction mode |
| `faction.unlocked` | faction unlocked |

## Not included (UI/visual only)

44 game events are excluded because they're pure UI, visual, or editor events with no gameplay value:

- Panel show/hide, selection, batch control (12)
- Tool enter/exit, tool groups (8)
- Camera level, water opacity, decals (5)
- Input, keybinds, keyword matching (3)
- Debug/dev mode toggles (2)
- Main menu, settlement relocation (2)
- Benchmark, Steam Workshop, tutorials, undo, preview (5)
- Automation building UI pins (3)
- Map editor events (4)

## Related

- [`websocket-protocol.md`](websocket-protocol.md) — authoritative wire contract (envelope, auth, reconnect)
- [`architecture.md`](architecture.md#websocket-protocol) — how the broadcaster fits into the mod
