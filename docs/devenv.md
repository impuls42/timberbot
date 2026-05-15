# Development Environment Setup

Requirements for building and testing Timberbot on **Ubuntu 24.04** (headless server or desktop) and **macOS** (Apple Silicon or Intel).

## Quick Check

Run these to verify your environment is ready:

```bash
dotnet --version          # ≥ 8.0 (for tests), any recent SDK for the mod (netstandard2.1)
python3 --version         # ≥ 3.10
pip show requests toons   # both installed
ilspycmd --version        # ≥ 9.0 (optional, for decompiling game DLLs)
```

---

## Required: .NET SDK

The mod targets `netstandard2.1` (C# 9). The test suite targets `net8.0`. The .NET SDK is backward-compatible, so **any SDK ≥ 8.0 works** — including .NET 9 or 10.

### Ubuntu 24.04

```bash
# Install latest available (recommended)
sudo apt update
sudo apt install -y dotnet-sdk-8.0    # or dotnet-sdk-10.0 if available

# Alternative: Microsoft install script (gets latest)
curl -sSL https://dot.net/v1/dotnet-install.sh | bash /dev/stdin
export PATH="$HOME/.dotnet:$PATH"
```

### macOS

```bash
brew install dotnet   # installs latest (10.x as of 2026)
# or pin a specific version:
brew install dotnet@8
```

### Verify

```bash
dotnet --list-sdks
# Any of 8.x, 9.x, 10.x works
```

---

## Required: Python 3.10+

The CLI client (`timberbot.py`) and its dependencies need Python 3.10+.

### Ubuntu 24.04

```bash
sudo apt install -y python3 python3-pip python3-venv
```

### macOS

```bash
# Usually pre-installed. Otherwise:
brew install python@3
```

### Create venv and install dependencies

```bash
cd /path/to/timberbot
python3 -m venv venv
source venv/bin/activate
pip install requests toons
```

The `requests` library is used for HTTP calls to the mod API. `toons` is used for persistent state serialization (`brain.toon` files).

---

## Required: Timberborn Game (v1.0+)

The C# mod compiles against game DLLs via `Publicize="true"` references. **The game must be installed** on the build machine so `dotnet build` can find the assemblies.

### Game DLL Location

The build looks for DLLs at `$(GameManagedDir)`. Default in the csproj:

```
C:\Games\Steam\steamapps\common\Timberborn\Timberborn_Data\Managed
```

Override it for your platform:

```bash
# macOS (Steam)
dotnet build -p:GameManagedDir="$HOME/Library/Application Support/Steam/steamapps/common/Timberborn/Timberborn.app/Contents/Resources/Data/Managed"

# Linux (Steam default)
dotnet build -p:GameManagedDir="$HOME/.steam/steam/steamapps/common/Timberborn/Timberborn_Data/Managed"

# Linux (Steam flatpak)
dotnet build -p:GameManagedDir="$HOME/.var/app/com.valvesoftware.Steam/.steam/steam/steamapps/common/Timberborn/Timberborn_Data/Managed"

# GOG (Linux)
dotnet build -p:GameManagedDir="$HOME/GOG Games/Timberborn/game/Timberborn_Data/Managed"
```

> **Headless server note:** If you're building on a CI/headless machine where Timberborn can't be installed via Steam, you can copy the `Managed/` directory (~200MB) from a machine that has the game. Only the DLLs are needed at compile time — the game doesn't need to run.

### Required DLLs at Compile Time

The following DLLs from the game's `Managed/` folder are referenced by the mod (see full list in `Timberbot.csproj`):

| Category | DLLs |
|---|---|
| Core framework | `Bindito.Core.dll`, `UnityEngine.CoreModule.dll`, `Newtonsoft.Json.dll` |
| Entity system | `Timberborn.BaseComponentSystem.dll`, `Timberborn.EntitySystem.dll`, `Timberborn.SingletonSystem.dll` |
| Buildings | `Timberborn.Buildings.dll`, `Timberborn.WaterBuildings.dll`, `Timberborn.BlockSystem.dll` |
| Game systems | `Timberborn.GameCycleSystem.dll`, `Timberborn.Goods.dll`, `Timberborn.WeatherSystem.dll`, etc. |
| **Automation (new)** | `Timberborn.Automation.dll`, `Timberborn.AutomationBuildings.dll` |
| UI & tools | `Timberborn.CoreUI.dll`, `Timberborn.ToolSystem.dll`, `UnityEngine.UIElementsModule.dll` |

### Mod Output Location

After a successful build, the mod auto-deploys to `$(ModDir)`:

```
~/Documents/Timberborn/Mods/Timberbot/
```

Override with `-p:ModDir=/custom/path` if needed.

---

## Required: NuGet Feed Access

The build uses `BepInEx.AssemblyPublicizer.MSBuild` from the BepInEx NuGet feed. The `nuget.config` in `timberbot/src/` already configures this:

```xml
<packageSources>
  <add key="nuget.org" value="https://api.nuget.org/v3/index.json" />
  <add key="BepInEx" value="https://nuget.bepinex.dev/v3/index.json" />
</packageSources>
```

`dotnet restore` pulls this automatically. No manual setup needed.

---

## Optional: ILSpy CLI (for decompiling game DLLs)

When the game updates and you need to inspect new internal APIs:

### Ubuntu 24.04

```bash
dotnet tool install -g ilspycmd
export PATH="$HOME/.dotnet/tools:$PATH"
```

### macOS

```bash
dotnet tool install -g ilspycmd
```

### Usage

```bash
# Decompile automation DLLs locally when you need to inspect API surface
ilspycmd "$GAME_MANAGED/Timberborn.Automation.dll" > /tmp/Timberborn.Automation.cs
ilspycmd "$GAME_MANAGED/Timberborn.AutomationBuildings.dll" > /tmp/Timberborn.AutomationBuildings.cs
```

Decompiled sources are not checked into the repo — generate them on demand from your installed game version.

---

## Optional: OpenCode (for AI-assisted development)

Two prompt sets ship with this repo:

- Gameplay prompts (`timberbot`, `scout`, `wirer`, `auditor`) live inside the
  `timberbot` Python package at `python/src/timberbot/agent_prompts/` and are
  exposed via `tbot init` / `tbot agent prompts`.
- The development-agent prompt (`beaver-developer.md`) lives at repo root
  under `agents/`. It targets *this codebase* (mod + CLI development), so it
  doesn't ship inside the `timberbot` wheel.

Both work with OpenCode, Claude Code, or any agent runner that loads a markdown system prompt.

### Install

```bash
# See https://opencode.ai/docs/ for latest install instructions
curl -fsSL https://opencode.ai/install | bash
```

### Configure

Point your agent runner at the relevant file:

- gameplay roles: `python/src/timberbot/agent_prompts/<role>.md`, or the
  editable copies created by `tbot init` in your user config dir.
- mod/CLI development: `agents/beaver-developer.md` at the repo root.

---

## Build & Test Commands

### Build the mod

```bash
cd timberbot/src
dotnet restore
dotnet build
# or with custom game path:
dotnet build -p:GameManagedDir="/path/to/Timberborn_Data/Managed"
```

### Run C# tests

```bash
cd timberbot/test
dotnet test
```

The test project references pure utility classes (`TimberbotJw.cs`, `TimberbotPure.cs`) that have no Unity/game dependencies, so tests run without the game installed.

### Run the Python CLI

```bash
pip install -e python/      # editable install from the repo
tbot ping
tbot buildings
tbot brain goal:"Keep beavers alive"
```

(Or `pipx install timberbot` to install the published wheel without a venv.)
The game must be running with the mod loaded for the CLI to connect (default: `http://localhost:8085`).

---

## Runtime: Testing the Mod In-Game

To test changes end-to-end:

1. Build the mod (`dotnet build` — auto-deploys to the mod folder)
2. Launch Timberborn
3. Enable `Timberbot API` in the Mod Manager
4. Load or start a game
5. Verify: `curl http://localhost:8085/api/ping` → `{"status": "ok", "ready": true}`
6. Use the CLI or call API endpoints directly

### Settings

The mod reads `settings.json` at startup:

```json
{
  "debugEndpointEnabled": false,
  "webhooksEnabled": true,
  "httpPort": 8085,
  "terminal": "wt -d {cwd} --",
  "pythonCommand": ""
}
```

- **httpPort**: Change if 8085 is taken
- **terminal**: Windows Terminal by default; macOS auto-detects Terminal.app
- **pythonCommand**: Override if Python isn't on PATH (e.g. `/usr/bin/python3`)

---

## Platform-Specific Notes

### macOS

- The game runs natively on Apple Silicon (no Rosetta needed since Timberborn 1.0)
- The mod's agent launcher (`TimberbotAgent.cs`) has a known bug around line 680 where it forces Windows-specific logic for terminal detection. See `AGENTS.md` for details.

### Linux (Ubuntu 24.04)

- Timberborn runs natively on Linux via Steam (Proton not required)
- Steam may use a compatibility layer anyway — check `~/.steam/steam/steamapps/common/Timberborn/` for native Linux binaries vs Proton prefix
- The `Managed/` DLL path is the same regardless of whether the game runs natively or via Proton

### Headless / CI

- Only `dotnet build` and `dotnet test` are needed — no game runtime required for compilation (as long as DLLs are present)
- Copy the `Managed/` folder from a machine with the game installed
- Python CLI tests that hit the API require a running game instance and are not suitable for headless CI
