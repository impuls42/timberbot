# Developing

## File structure

```
TimberbornMods/
  timberbot/
    src/                              C# mod (runs inside the game)
      TimberbotService.cs               Lifecycle, settings, orchestration (7 DI params)
      TimberbotEntityRegistry.cs        GUID-backed entity lookup + numeric-ID bridge (4 DI params)
      TimberbotReadV2.cs                All GET read endpoints, tracked refs, and published snapshots
      TimberbotWrite.cs                 All POST write endpoints (22 DI params)
      TimberbotPlacement.cs             Building placement, path routing, terrain (14 DI params)
      TimberbotEvents.cs                [OnEvent] publishers → WS broadcaster (5 DI params)
      TimberbotDebug.cs                 Reflection inspector and benchmark (1 DI param)
      ITimberbotWriteJob.cs              Write job interface for budgeted main-thread execution
      TimberbotHttpServer.cs            HttpListener, routing, request/response handling
      TimberbotJw.cs                    Fluent zero-alloc JSON writer
      TimberbotPure.cs                  Pure static helpers (no Unity deps, shared with test project)
      TimberbotLog.cs                   File-based error logging, timestamped, thread-safe
      TimberbotConfigurator.cs          Bindito DI module registration
      TimberbotAutoLoad.cs              Auto-load a save at main menu via autoload.json or CLI args
      TimberbotAutoLoadConfigurator.cs  MainMenu context DI registration for auto-load
      Timberbot.csproj                  Build config, game DLL references
      manifest.json                     Mod metadata (version, name, description)
      settings.json                     Persistent settings store (runtime + agent/UI settings, primarily edited in-game)
      thumbnail.png                     Mod thumbnail (rendered in the Mod Manager + README)
    test/
      Timberbot.Tests.csproj            xUnit test project (net8.0, shares source files)
      TimberbotJwTests.cs               JSON writer tests (primitives, nesting, commas, reuse)
      TimberbotPureTests.cs             Pure helper tests (parsing, quoting, assertions, normalization)
    script/
      release.py                        Build + package + GitHub release script
  python/                               `tbot` CLI + typed Pydantic client (PyPI: `timberbot`)
    src/timberbot/                      package source
    tests/                              pytest suites (unit + httpserver-mocked + integration)
    scripts/                            codegen / fixture-capture helpers
  docs/                               Documentation
    architecture.md                     How the mod works (thread model, caching, serialization)
    performance.md                      Measurements, benchmarks, GC pressure, optimization history
    developing.md                       This file (building, testing, contributing)
    api-reference.md                    Endpoint documentation
```

## Building the mod

Requires .NET SDK 6+ and Timberborn installed.

```bash
cd timberbot/src
dotnet build
```

This compiles `Timberbot.dll` and auto-deploys to:

| Platform | Default `$(ModDir)` |
|----------|--------------------|
| Windows / macOS / native Linux | `~/Documents/Timberborn/Mods/Timberbot/` |
| Linux + Proton (auto-detected) | `~/.steam/steam/steamapps/compatdata/1062090/pfx/drive_c/users/steamuser/Documents/Timberborn/Mods/Timberbot/` |

Game DLLs are referenced from:
```
C:\Games\Steam\steamapps\common\Timberborn\Timberborn_Data\Managed
```

If your Steam install is elsewhere, override `GameManagedDir` when building instead of editing the project file:

```bash
dotnet build /p:GameManagedDir="D:\Steam\steamapps\common\Timberborn\Timberborn_Data\Managed"
dotnet build /p:ModDir="C:\Users\<you>\Documents\Timberborn\Mods\Timberbot"
```

On macOS, pass the platform-specific `GameManagedDir` and `ModDir` the same way.

For Linux/Proton setups with non-`steamuser` Wine usernames or custom prefixes, the bundled helper resolves the right ModDir via the same Python logic the CLI uses:

```bash
scripts/deploy.sh                # auto-detect (honors $TBOT_DOCUMENTS_DIR)
scripts/deploy.sh /custom/mods   # explicit override
```

## How the mod works

1. `TimberbotConfigurator` registers all services as singletons in the `Game` context via Bindito DI
2. On `Load()`, `TimberbotService` starts an `HttpListener` on port 8085 in a background thread and (when `wsEnabled`) a separate `TcpListener` on port 8086 for the WebSocket channel
3. GET requests are handled directly on the background listener thread (reads from `ReadV2` published snapshots)
4. POST requests are queued in a `ConcurrentQueue<PendingRequest>` and drained on the main thread
5. `UpdateSingleton()` runs every frame: drains POST queue, services pending fresh publishes
6. Game events (`[OnEvent]` handlers in `TimberbotEvents.cs`) and state mutations (`TimberbotAgentState.Changed`) fan out to the WS broadcaster, which pushes `event` / `state` frames to every connected client

For full architecture details see [architecture.md](architecture.md).

## Settings model

The in-game `Settings` modal is the primary configuration surface for Timberbot. For the full key-by-key schema of every config file, see the [Configuration Reference](configuration.md).

All settings persist to `settings.json`, including:

- runtime settings: `httpPort`, `wsPort`, `wsEnabled`, `listenAddress`, `authToken`, `debugEndpointEnabled`, `maxBodyBytes`, `writeBudgetMs`
- widget UI settings such as `widgetLeft`, `widgetTop`, `actionLoggingEnabled` (per-backend model/effort/template now live in `~/.config/timberbot/config.toml`; the persisted `mode`/`goal` live in `state.json` alongside `settings.json` and are owned by `TimberbotAgentState`)

`TimberbotService` keeps an in-memory settings object and debounces writes back to disk. Editing `settings.json` directly is supported, but it is the manual/advanced path rather than the default workflow.

## Adding a new GET endpoint

1. Add a `Collect*` method to `TimberbotReadV2.cs`
2. Add the route to `RouteRequest()` in `TimberbotHttpServer.cs`
3. If you need new game services, inject them via the `TimberbotReadV2` constructor
4. Add a matching method to `TimberbotClient` in `python/src/timberbot/api/client.py`

## Adding a new POST endpoint

1. Add an action method to `TimberbotWrite.cs` or `TimberbotPlacement.cs`
2. Add the route to `RouteRequest()` in `TimberbotHttpServer.cs` (POST routes run on main thread)
3. If you need new game services, inject them via the constructor
4. Add a matching typed method to `TimberbotClient` in `python/src/timberbot/api/client.py` (the operationId in `openapi.yaml` and the method name must agree — the contract tests will catch drift)

## Adding new game DLL references

```xml
<Reference Include="Timberborn.NewSystem" Publicize="true">
  <Private>false</Private>
  <HintPath>$(GameManagedDir)\Timberborn.NewSystem.dll</HintPath>
</Reference>
```

`Publicize="true"` makes internal types accessible. `<Private>false</Private>` prevents copying the DLL to output (the game already has it).

## CI artifacts (testing a PR locally)

Every PR that touches the relevant code paths uploads downloadable artifacts to its workflow runs. Open the PR's **Checks** tab → click the relevant workflow run → scroll to **Artifacts** at the bottom.

| Workflow | Artifact name | Contains | Trigger |
|---|---|---|---|
| Python tests | `timberbot-py-<pr-number>` | Wheel + sdist for the proposed CLI | Any change under `python/` |
| docs | `docs-site-<pr-number>` | Fully-built MkDocs site (open `index.html` locally) | Any change under `docs/` or `mkdocs.yml` |

To test the proposed Python CLI from a PR:

```bash
# download `timberbot-py-<pr>.zip` from the PR's Checks tab, then:
unzip timberbot-py-<pr>.zip -d /tmp/tb
pipx install --force /tmp/tb/timberbot-*.whl
tbot --help
```

**C# mod DLL is not in CI.** The mod links against ~80 proprietary `Timberborn.*.dll` files from your Steam install, so GitHub Actions cannot produce a runnable `Timberbot.dll`. To test a proposed mod-side change locally, check out the PR branch and rebuild — the post-build target auto-deploys to your Timberborn Mods folder:

```bash
gh pr checkout <pr-number>          # or: git fetch origin pull/<pr>/head:pr-<pr> && git checkout pr-<pr>
cd timberbot/src
dotnet build                         # also auto-deploys to ~/Documents/Timberborn/Mods/Timberbot
```

## Testing

### Unit tests

Offline tests for the JSON serializer and pure helper functions. No game required.

```bash
dotnet test timberbot/test/
```

141 tests covering `TimberbotJw` (serialization, comma handling, nesting, reuse) and `TimberbotPure` (orientation parsing, name cleanup, assertion evaluation, value normalization, shell quoting).

### Test suite

The legacy `timberbot/script/test_*.py` harnesses have been migrated into the
pytest tree under `python/tests/`. Unit tests run without a game; the
`integration` marker covers the live-game smoke, validation, and performance
suites.

```bash
# unit tests only (default; no game required)
python -m pytest python/tests/

# integration tests against a running Timberborn + Timberbot mod
python -m pytest python/tests/integration/ -m integration

# specific integration subsets
python -m pytest python/tests/integration/ -m "integration and write"
python -m pytest python/tests/integration/ -m "integration and slow"
```

See `python/tests/integration/README.md` for the runner-level options
(group selection, performance budgets, benchmark iterations).

| Group | Tests | Description |
|---|---|---|
| read | 9 | GET endpoints, projections, map, schema, data accuracy |
| write | 16 | speed, pause, priority, workers, floodgate, recipes, etc. |
| placement | 6 | place/demolish, orientation, find, water, overridable, blockers |
| path | 16 | flat, 1z, 2z (all directions), A* diagonal/obstacle/no-route, sections |
| crops | 6 | crops, tree marking, planting, clear, demolish crop |
| buildings | 6 | detail, inventory, range, recipes, prefab costs, power |
| beavers | 10 | detail, needs, position, district, bots, carrying, durability |
| events | 1 | WS frame fan-out, `tbot listen` end-to-end |
| cli | 2 | CLI commands, error codes |
| perf | 4 | endpoint latency, building perf, brain perf, v2 parity |
| wipe | 1 | demolish all buildings + clear all crops |

### What the tests cover

- **Smoke**: representative coverage of the full `/api/*` read surface
- **Write-to-read**: POST change -> first GET sees it -> restore -> first GET sees restoration
- **Performance**: direct endpoint latency comparisons across the live snapshot path
- **Concurrency**: simultaneous requests against projection-backed endpoints
- **Validation**: `test_validation.py` covers 77 tests across 11 groups (read, write, placement, path, crops, buildings, beavers, events, cli, perf, wipe)
- **Cache invalidation**: place path -> count+1, demolish -> count back (EventBus + fresh-on-request snapshots)
- **Data accuracy**: `validate` endpoint compares cached vs live game state per field. `validate_all` checks all entities, all fields, 0 mismatches
- **Burst**: 7 sequential calls < 3s total (24ms measured)
- **Save-agnostic**: discovery phase detects faction, map bounds, existing buildings
- **Events (WebSocket)**: `tbot listen` connects to `ws://host:wsPort/api/ws`, receives `event` frames as in-game state changes

### In-game benchmark

`/api/benchmark` (POST, requires `debugEndpointEnabled: true` in settings.json) runs micro-benchmarks with GC0 tracking. It is queued and stepped under the main-thread write budget, so the request may take multiple frames to complete.

- Collection iteration patterns (foreach vs for-loop, enumerator boxing)
- Game API alloc checks (GetNeeds, Inventories, BreedingPod.Nutrients)
- Lightweight internal helpers like prefab collection

Results include per-test GC0 count, ms/call, and pass/fail. See [performance.md](performance.md#benchmarks) for recorded results.

## Release

This fork ships via GitHub releases only; the Steam Workshop entry "Timberbot API" belongs to the upstream [`abix-/TimberbornMods`](https://github.com/abix-/TimberbornMods) project. Users install the fork manually — see [Getting Started](getting-started.md#install-the-mod).

### GitHub release

```bash
python timberbot/script/release.py --release
```

This builds a Release DLL, packages a ZIP (`Timberbot.dll` + `manifest.json` + `thumbnail.png`), tags the version, and creates a GitHub release. The `tbot` CLI ships separately on PyPI as the `timberbot` package and is not bundled into the mod ZIP.
