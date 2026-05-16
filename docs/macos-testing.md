# macOS Testing

Use this checklist when validating Timberbot on macOS. The post-WS-rework flow is the same as Windows/Linux except where called out.

## Prerequisites

- Timberborn running on macOS (Apple Silicon native — no Rosetta needed since Timberborn 1.0)
- Python 3.10+ on PATH (`brew install python` or any pyenv install)
- A working `claude`, `codex`, or `opencode` CLI on PATH (whichever backend you'll test)

## Install the mod (manual, GitHub release)

The Steam Workshop entry "Timberbot API" is the upstream [`abix-/TimberbornMods`](https://github.com/abix-/TimberbornMods) project; this fork ships only via [GitHub releases](https://github.com/impuls42/timberbot/releases). Download `Timberbot.dll`, `manifest.json`, `thumbnail.png` and place them under:

```
~/Documents/Timberborn/Mods/Timberbot/
```

If the upstream Workshop mod is already subscribed, disable or unsubscribe it before launching — both registering the same singleton would prevent the mod from loading.

## Install the CLI

```bash
pipx install timberbot
tbot init
```

## Primary test flow

1. Start the connector in a terminal and leave it running:
   ```bash
   tbot watch
   ```
   Expected: logs `wsclient: connected to ws://127.0.0.1:8086/api/ws` once the game is in a save.
2. Launch Timberborn.
3. Dismiss the Mod Manager dialog; load any save.
4. Confirm the green `Timberbot API` widget appears in the bottom-right corner. The state pill should read **Not Ready** (yellow) with the banner "Connected to game session — waiting for player to Launch."
5. Press **Launch** in the widget.
6. Expected: pill flips to **Idle** (green); `tbot watch` log shows a new `ws frame type=state` within ~50 ms; the connector dispatches the configured backend.

## Stop behavior

1. Press **Stop** in the widget.
2. Expected: pill flips back to **Not Ready** (yellow); subsequent `curl http://127.0.0.1:8085/api/buildings` returns `409 game_not_ready`; `tbot watch` aborts any in-flight cycle and idles until the next Launch.

## Save autoload helper

`tbot launch` on macOS writes an `autoload.json` but doesn't open the game itself (Steam URL-handler launch isn't reliable on Mac from a CLI process):

```bash
tbot launch settlement:<name>
```

Expected:
- Writes `autoload.json` to the mod folder.
- Prints a one-line note that the user must open Timberborn manually.

Then open Timberborn yourself; the selected save auto-loads at the main menu.

## What to send back if anything fails

Please include:

- macOS version (`sw_vers`)
- which backend (`claude` / `codex` / `opencode`)
- the `tbot watch` stderr output around the failure
- this log file:

```bash
~/Documents/Timberborn/Mods/Timberbot/timberbot.log
```
