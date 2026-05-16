# Webhooks

> **v0.9 — architecture rework, in flight.** Behavior on `master` may briefly lag this page.

Push notifications for game events. Instead of polling, the mod sends HTTP POST requests to your registered URLs when events happen in-game.

Webhooks are unaffected by the [ready gate](architecture.md#ready-gate) — they keep firing whether the player has pressed Launch or not. That's deliberate: a game-event subscriber (a Discord bot, a dashboard, an alerting webhook) should still see what's happening in the colony even when the AI is muted.

## Local listener quickstart (`tbot listen`)

> **`tbot listen` is now a pure WebSocket subscriber.** The inbound aiohttp.web
> receiver and its `--port` / `--host` bind flags were removed in the v0.9
> rework — agents and viewers connect to the mod's `/api/ws` channel instead
> of hosting an HTTP server. See the WS quickstart for details (full guide
> follows in the WS-channel docs unit).

## Setup

1. Configure in `settings.json` (see [architecture.md](architecture.md#settings) for all options):
   - `webhooksEnabled`: enable/disable (default true)
   - `webhookBatchMs`: batching window in ms (default 200, 0 = immediate)
   - `webhookCircuitBreaker`: consecutive failures before auto-disable (default 30)

2. Register a webhook:
```bash
tbot register_webhook url:http://localhost:9000/events events:drought.start,drought.end,beaver.died
```

Or via API:
```
POST /api/webhooks
{ "url": "http://localhost:9000/events", "events": ["drought.start", "drought.end"] }
```

Omit `events` to receive all events.

3. Your server receives batched POST requests (JSON array):
```json
[
  {"event": "drought.start", "day": 45, "timestamp": 1711300000, "data": {"duration": 8}},
  {"event": "beaver.died", "day": 45, "timestamp": 1711300000, "data": null}
]
```

Each POST contains an array of events that accumulated during the batch window. Single events arrive as a 1-element array.

## Management

```bash
tbot list_webhooks                         # GET /api/webhooks
tbot unregister_webhook id:wh_1            # POST /api/webhooks/delete
```

Webhooks are stored in memory. they reset on game restart. Re-register on startup.

## Connector triggers

The agent connector (`tbot watch`) uses a second, dedicated push channel — separate from the regular event webhooks above. On connect it calls:

```
POST /api/tbot/register
{ "webhook_url": "http://127.0.0.1:9000/agent" }
```

The mod stores that URL in `tbotWebhookUrl` (cleared if heartbeats lapse for 6 s). When the player presses **Launch** in request mode, the mod fires a synthetic `agent.request` event at the registered URL as the *fast path* trigger. If the connector is offline or the URL is stale, the request still surfaces via the heartbeat poll response as the *slow path*.

Connector triggers are not part of the 68-event game-event catalog below — they're a separate channel scoped to the agent.

## Events (68 total)

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

## Circuit breaker

After 30 consecutive delivery failures (configurable via `webhookCircuitBreaker`), a webhook is automatically disabled. Check status via `GET /api/webhooks`. disabled webhooks show `"disabled": true` and `"failures": 30`. Re-register to reset.

For webhook internals (batching, threading, circuit breaker implementation) see [architecture.md](architecture.md#timberbotwebhook).
