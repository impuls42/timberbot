timberbot
=========

Python client and `tbot` CLI for the
[Timberbot](https://github.com/impuls42/timberbot) Timberborn mod HTTP API.
Talks to the C# mod running inside the game on `127.0.0.1:8085`.

Install
-------

```
pipx install timberbot
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
tbot place_building --prefab=Path --x=120 --y=130 --z=2 --orientation=south
tbot top
tbot --help
```

The `tbot` command is the entry point for everything; it is dispatched via
[python-fire](https://github.com/google/python-fire), so each `TimberbotClient`
method becomes a subcommand. Run with no args (or `--help`) to list every
command; run `tbot <command> --help` for per-command flags and positional args.

Layout
------

- `timberbot.api.client.TimberbotClient` — pure HTTP client, one method per endpoint.
- `timberbot.state.SettlementContext` — per-settlement persistent memory (`brain.toon`).
- `timberbot.formatters` — colors, tables, ASCII map, live dashboard renderer.
- `timberbot.cli` — global-flag parsing + the python-fire `Tbot` class that exposes every client method.

The `from timberbot import Timberbot` alias re-exports `TimberbotClient`.
