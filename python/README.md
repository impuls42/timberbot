tbot
====

Python client and CLI for the [Timberbot](https://github.com/impuls42/timberbot)
Timberborn mod HTTP API. Talks to the C# mod running inside the game on
`localhost:8085`.

Install
-------

```
pip install tbot
```

Or from source:

```
pip install -e python/
```

Use
---

```
tbot summary
tbot buildings
tbot place_building prefab:Path x:120 y:130 z:2 orientation:south
tbot top
tbot --help
```

The `tbot` command is the entry point for everything. Run with no args to list
all commands; run with `--help` for global flags.

Layout
------

- `tbot.api.client.TimberbotClient` — pure HTTP client, one method per endpoint.
- `tbot.state.SettlementContext` — per-settlement persistent memory (`brain.toon`).
- `tbot.formatters` — colors, tables, ASCII map, live dashboard renderer.
- `tbot.cli` — argv parsing, command registry, main entry point.

The legacy alias `from tbot import Timberbot` re-exports `TimberbotClient`.
