# Timberbot API

<p align="center">
  <img src="thumbnail.png" alt="Timberbot — a cybernetic beaver playing Timberborn at a desk" width="320">
</p>

**Full read/write HTTP API for controlling Timberborn with AI.**

Timberbot gives Claude, Codex, ChatGPT, or your own scripts complete access to a running Timberborn colony over HTTP. Read game state, place buildings, manage workers, plant crops, wire automation, and keep your beavers alive.

!!! info "Modified fork"
    This project is a modified fork of [abix-/TimberbornMods](https://github.com/abix-/TimberbornMods). It extends the original mod with an expanded read/write HTTP API, automation wiring endpoints, webhooks, and AI-agent integrations. All credit for the original mod goes to [abix-](https://github.com/abix-).

---

## Start here

<div class="grid cards" markdown>

- **[Getting Started](getting-started.md)**

    Install the mod, set up the Python client, and run your first commands.

- **[API Reference](api-reference.md)**

    Every HTTP endpoint with request/response examples.

- **[Timberbot Guide](timberbot.md)**

    Full operating guide for AI agents playing Timberborn.

- **[Features](features.md)**

    Compatibility matrix of what's implemented vs gaps.

- **[Webhooks](webhooks.md)**

    Subscribe to game events over HTTP.

- **[Architecture](architecture.md)**

    Internals, thread model, read/write pipeline.

</div>

---

## What you can do

| | Read | Write |
|---|---|---|
| **Buildings** | All buildings with workers, power, priority, inventory | Place, demolish, pause, configure |
| **Beavers** | Wellbeing, needs, workplace, contamination | Migrate between districts (in-progress) |
| **Resources** | Per-district stocks, distribution settings | Set import/export, stockpile config |
| **Map** | Terrain, water, occupants, contamination | Plant crops, mark trees, route paths |
| **Automation** | Sensors, relays, memory cells, wiring graph | Link/unlink inputs, configure thresholds and modes |
| **Colony** | Weather, science, alerts, notifications | Speed, work hours, unlock buildings |

---

## Quick taste

```bash
# with Timberborn running + the mod loaded
timberbot.py summary                              # colony snapshot
timberbot.py map x1:110 y1:130 x2:130 y2:150      # ASCII map with terrain shading
timberbot.py place_path x1:110 y1:130 x2:130 y2:150  # A* pathfinding with auto-stairs
timberbot.py set_speed speed:3                    # fast forward

# or raw HTTP, no Python required
curl http://localhost:8085/api/summary
curl -X POST http://localhost:8085/api/speed -d '{"speed": 3}'
```

Ready to install? Head to **[Getting Started](getting-started.md)**.
