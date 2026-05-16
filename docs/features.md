# Features

| System | What you get | Status |
|---|---|---|
| [Beavers](api-reference.md#get-apibeavers) | Per-beaver position, district, wellbeing, needs, workplace, carried goods, deterioration. `detail:full` for all needs with group category | Yes |
| [Wellbeing](api-reference.md#get-apiwellbeing) | Population wellbeing by category (Social, Fun, Nutrition, Aesthetics, Awe) with current/max | Yes |
| [Buildings](api-reference.md#get-apibuildings) | Workers, priority, power, reachability, inventory, construction progress | Yes |
| [Placement](api-reference.md#post-apibuildingplace) | Place any building with game-native validation. Find spots with entrance coords, reachability, power, water depth | Yes |
| [Building range](api-reference.md#post-apibuildingrange) | Work radius for farmhouse, lumberjack, forester, gatherer, scavenger, district center | Yes |
| [Paths](api-reference.md#post-apipathroute) | Auto-stairs and platforms across z-levels | Yes |
| [Crops](api-reference.md#post-apiplantingmark) | Plant, clear, find valid irrigated spots within farmhouse range | Yes |
| [Trees](api-reference.md#get-apitrees) | Growth, cutting marks, densest clusters | Yes |
| [Tiles](api-reference.md#post-apitiles) | Terrain, water, badwater, moisture, occupants. [ASCII map](api-reference.md#map) with height shading | Yes |
| [Weather](api-reference.md#get-apiweather) | Drought countdown, badtide status | Yes |
| [Power](api-reference.md#get-apibuildings) | Per-building input/output, generator/consumer, powered state | Yes |
| [Science](api-reference.md#get-apiscience) | Points, unlock costs, unlock with one call | Yes |
| [Workers](api-reference.md#post-apiworkers) | Count, priority, pause, haul priority | Yes |
| [Distribution](api-reference.md#get-apidistribution) | Import/export per good, beaver migration between districts | Yes |
| [Production](api-reference.md#post-apirecipe) | Recipes, farmhouse action, forester priority | Yes |
| [Stockpiles](api-reference.md#post-apistockpilecapacity) | Capacity and allowed goods per stockpile | Yes |
| [Floodgates](api-reference.md#post-apifloodgate) | Water gate height | Yes |
| [Work schedule](api-reference.md#post-apiworkhours) | Work end hour | Yes |
| [Game speed](api-reference.md#post-apispeed) | Pause, normal, fast, fastest | Yes |
| [Alerts](api-reference.md#get-apialerts) | Unstaffed, unpowered, unreachable buildings | Yes |
| [Map](api-reference.md#map) | ASCII map with terrain height shading and building markers | Yes |
| [Summary](api-reference.md#get-apisummary) | Entire colony snapshot in one call | Yes |
| [Debug](api-reference.md#post-apidebug) | Reflection-based inspection of any game object at runtime | Yes |
| [Building costs](api-reference.md#get-apiprefabs) | Material costs, science cost, unlock status per building | Yes |
| [Inventory](api-reference.md#get-apibuildings) | Per-building stock and capacity for tanks, warehouses, stockpiles | Yes |
| [Recipes](api-reference.md#get-apibuildings) | Available recipes and current recipe on manufactories | Yes |
| [Breeding](api-reference.md#get-apibuildings) | Breeding pod nutrient status | Yes |
| [Beaver activity](api-reference.md#get-apibeavers) | Current status from game's status system | Yes |
| [Clutch](api-reference.md#post-apibuildingclutch) | Engage/disengage clutch on buildings | Yes |
| [Bot condition/fuel](api-reference.md#get-apibeavers) | Bot needs (Energy, ControlTower, Grease) via beavers endpoint | Yes |
| [Beaver position](api-reference.md#get-apibeavers) | Per-beaver x,y,z grid coordinates | Yes |
| [Water depth](api-reference.md#post-apimap) | Water height float per tile for drought planning | Yes |
| [District per beaver](api-reference.md#get-apibeavers) | Which district each beaver/bot belongs to | Yes |
| [Badwater tiles](api-reference.md#post-apimap) | Contamination level per map tile (badwater + soil) | Yes |
| [Map stacking](api-reference.md#post-apimap) | Multiple occupants at different z-levels on same tile | Yes |
| [Carried goods](api-reference.md#get-apibeavers) | What each beaver is hauling, lifting capacity, overburdened state | Yes |
| [Power networks](api-reference.md#get-apipower) | Connected building groups with supply vs demand per network | Yes |
| [Bot durability](api-reference.md#get-apibeavers) | Deterioration progress (0-1) on mechanical beavers | Yes |
| [Resource projection](api-reference.md#get-apisummary) | Projected days of logs, planks, gears (logDays, plankDays, gearDays) | Yes |
| [Event stream](events.md) | 68 push-notification game events (drought, death, construction, weather, power, wonders) delivered over the mod's WebSocket | Yes |
| [In-game agent widget](getting-started.md#start-a-session) | Movable corner widget with `Launch`, `Stop`, mode dropdown, prompt/goal textarea, and live agent status | Yes |
| [In-game settings](getting-started.md#settings-and-configuration-server-mod) | Primary configuration surface for runtime + security settings. All changes persist to `settings.json` | Yes |
| [`tbot watch` agent connector](getting-started.md#start-a-session) | Long-running Python process that holds a WebSocket to the mod, reacts to `state` frames, and dispatches the agent on Launch | Yes |
| [Pagination](api-reference.md#pagination) | Server-side limit/offset on all list endpoints (default 100 items) | Yes |
| [Filtering](api-reference.md#filtering) | Server-side name and proximity filtering on list endpoints | Yes |
| [Error codes](api-reference.md#error-format) | Structured `"code: detail"` error format with machine-readable prefixes | Yes |
| [Faction detection](api-reference.md#get-apisummary) | Auto-detects Folktails vs Iron Teeth, uses correct prefab names | Yes |
| [Brain](api-reference.md#spatial-memory-cli-only) | Live summary + persistent goal/tasks/locations for `/timberbot` and other advanced CLI workflows | Yes |
| [Placement distance](api-reference.md#post-apiplacement-find) | Flow-field path distance from DC on find_placement results for optimal building placement | Yes |
| [Path routing](api-reference.md#post-apipathroute) | Auto-stairs and platforms across z-levels, checks science unlocks | Yes |
| [Crops endpoint](api-reference.md#get-apicrops) | Separate crops listing with growth status, independent from trees | Yes |
| [Food clusters](api-reference.md#get-apifood_clusters) | Top gatherable food clusters by density, excluding trees | Yes |
| [Settlement name](api-reference.md#get-apisettlement) | Save game / settlement name for per-settlement memory folders | Yes |
| [Remote connection](getting-started.md#remote-connections) | Connect external clients with `--host=` and `--port=` CLI flags, `TBOT_HOST`/`TBOT_PORT` env vars, or `[client]` in `config.toml` | Yes |
| [Benchmark](api-reference.md#post-apibenchmark) | Per-endpoint performance profiling | Yes |
| [Automation wiring](api-reference.md#post-apiautomationlink) | Wire sensors to relays to buildings, configure thresholds and modes | Yes |
| [Automation config](api-reference.md#post-apiautomationconfigure) | Set sensor thresholds, counter modes, relay logic, lever state | Yes |
