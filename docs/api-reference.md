# Timberbot API Reference

## Overview

| | |
|---|---|
| **Base URL** | `http://127.0.0.1:8085` |
| **Content-Type** | `application/json` |
| **Authentication** | Opt-in. None by default; when `authToken` is set in `settings.json` (or `TBOT_AUTH_TOKEN` env / `[client].auth_token` in `~/.config/timberbot/config.toml` on the client side), every `/api/*` route except `/api/ping` requires `Authorization: Bearer <token>`. Missing or wrong tokens get `401` with `WWW-Authenticate: Bearer realm="timberbot"`. |
| **CORS** | `Access-Control-Allow-Origin: *` |

### Output Format

All endpoints support two output formats via `?format=` query param or `"format"` in POST body.

| Format | Description |
|--------|-------------|
| `toon` | Default. Compact CSV tables via [TOON format](https://github.com/toon-format/toon). Requires `pip install toons` |
| `json` | Full nested data for programmatic access |

**Uniform schema:** every object in an array has identical keys in both formats. Missing values get defaults (`""`, `0`). **Booleans are 0/1 integers**, not true/false.

### Error Format

All errors return JSON with an `error` field in `"code: detail"` format. Every error is designed to be actionable: it tells you what went wrong, echoes what you sent, and tells you what to do next.

```json
{"error": "not_found: no entity with this id", "id": -12345}
{"error": "invalid_type: not a floodgate. use buildings name:Floodgate to find floodgates", "id": -12345, "name": "LumberjackFlag"}
{"error": "invalid_param: speed must be 0-3 (0=pause, 1=normal, 2=fast, 3=fastest)", "got": 5}
{"error": "insufficient_science: not enough science points to unlock", "building": "LargePowerWheel", "scienceCost": 60, "currentPoints": 10}
{"error": "not_found: source district does not exist", "from": "Bad Name", "districts": ["District 1", "District 2"]}
{"error": "not_found: recipe does not exist for this building", "recipeId": "Bad", "available": ["PlankRecipe", "TreatedPlankRecipe"]}
```

The error string starts with a machine-readable code. Parse the prefix before `:` to switch on it. Everything after `:` is human context.

| Code prefix | Meaning | Extra fields |
|------|---------|-------------|
| `not_found` | Entity, building, or district does not exist | `id`, `districts`, `available`, `building` |
| `invalid_prefab` | Prefab name does not exist. Suggests similar names | `prefab` |
| `invalid_type` | Entity exists but wrong type for this operation | `id`, `name` |
| `invalid_param` | Parameter value out of range or invalid | `got` |
| `not_unlocked` | Building requires science unlock first | `prefab`, `scienceCost`, `currentPoints` |
| `insufficient_science` | Not enough science points | `building`, `scienceCost`, `currentPoints` |
| `no_population` | No beavers available to migrate | `from`, `available`, `requested` |
| `disabled` | Feature disabled in settings.json | |
| `unknown_endpoint` | Route not found | `get_endpoints`, `post_endpoints` |
| `invalid_body` | Malformed JSON request body | |
| `operation_failed` | Game service threw an exception | varies |
| `internal_error` | Unhandled server error | |

### Python CLI

All HTTP endpoints are accessible via the Python client:

```bash
tbot <command>              # TOON format
tbot --json <command>       # JSON format
tbot <command> key:value    # with parameters
tbot --host=192.168.1.50 --port=8085 summary  # remote connection
```

The in-game Timberbot widget is the primary way to launch and configure the built-in agent. The Python CLI remains available for direct API access and advanced workflows.

### Pagination

List endpoints (buildings, beavers, trees, crops, gatherables, alerts, notifications) support server-side pagination via query params:

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 100 | Max items to return. `0` = unlimited (all items) |
| `offset` | 0 | Skip first N items |

When `limit > 0`, response wraps in a metadata object:

```json
{"total": 200, "offset": 50, "limit": 10, "items": [...]}
```

When `limit=0`, returns a flat array (backward compatible):

```json
[{...}, {...}, ...]
```

The Python CLI passes `limit=0` by default (AI/scripts typically want all data).

### Server-side Filtering

List endpoints also support server-side filtering via query params (also available as CLI params):

| Param | Description |
|-------|-------------|
| `name` | Case-insensitive substring match on entity name |
| x | X coordinate for proximity filter (requires y) |
| y | Y coordinate for proximity filter (requires x) |
| radius | Manhattan distance radius (requires x and y). Default: 30 |

Filters apply BEFORE pagination. The `total` in paginated responses reflects filtered count.

```
GET /api/buildings?name=Farm                    # all FarmHouses
GET /api/buildings?x=120&y=140&radius=20        # buildings near (120,140)
GET /api/trees?name=Pine&limit=10               # first 10 pine trees
GET /api/beavers?name=Bot&limit=0               # all bots (unlimited)
```

**CLI equivalent:**

```bash
tbot buildings name:Farm                # all FarmHouses
tbot buildings x:120 y:140 radius:20    # buildings near (120,140)
tbot trees name:Pine limit:10           # first 10 pine trees
tbot beavers name:Bot                   # all bots (unlimited)
```

---

## Game State

### GET /api/ping

Health check. Answered on listener thread (works even when game is paused/loading).

**CLI:** `tbot ping`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| status | string | Always `"ok"` |
| ready | bool | Always `true` |

```json
{"status": "ok", "ready": true}
```

---

### GET /api/summary

Full game state snapshot: settlement, time, weather, population, resources, trees (with species), crops (with species), housing, employment, wellbeing (with per-category breakdown), science, alerts, faction, DC location, building counts by role, and nearby tree/food clusters.

**CLI:** `tbot summary` | `tbot --json summary`

#### Response (format=json)

| Field | Type | Description |
|-------|------|-------------|
| settlement | string | Save game / settlement name |
| faction | string | `"Folktails"` or `"IronTeeth"` |
| time | object | See [GET /api/time](#get-apitime). Includes `speed` (0-3) |
| weather | object | See [GET /api/weather](#get-apiweather) |
| districts | array | Per-district: population, resources, housing, employment, wellbeing, DC |
| districts[].resources | object | Flat totals per good: `{"Water": 150, "Log": 80}` |
| districts[].housing | object | `{occupiedBeds, totalBeds, homeless}` per district |
| districts[].employment | object | `{assigned, vacancies, unemployed}` per district |
| districts[].wellbeing | object | `{average, miserable, critical}` per district |
| districts[].dc | object | `{x, y, z, orientation, entranceX, entranceY}` district center location |
| trees | object | `{markedGrown, markedSeedling, unmarkedGrown, species:[{name, markedGrown, unmarkedGrown, seedling}]}` |
| crops | object | `{ready, growing, species:[{name, ready, growing}]}` |
| wellbeing | object | `{average, miserable, critical, categories:[{group, current, max}]}` (global) |
| science | int | Current science points |
| alerts | object | `{unstaffed, unpowered, unreachable}` counts |
| buildings | object | Building counts by role: `{housing, wood, storage, power, food, water, ...}` |
| treeClusters | array | Nearby tree clusters (within 40 tiles of DC, same z). Same format as [tree_clusters](#get-apitree_clusters) |
| foodClusters | array | Nearby food clusters (within 40 tiles of DC, same z). Same format as [food_clusters](#get-apifood_clusters) |

??? example "Example response"

    ```json
    {
      "settlement": "My Colony",
      "faction": "IronTeeth",
      "time": {"dayNumber": 42, "dayProgress": 0.65, "partialDayNumber": 42.65, "speed": 2},
      "weather": {"cycle": 3, "cycleDay": 5, "isHazardous": false, "temperateWeatherDuration": 12, "hazardousWeatherDuration": 6, "cycleLengthInDays": 18},
      "districts": [{"name": "District 1", "population": {"adults": 20, "children": 5, "bots": 2}, "resources": {"Water": 150, "Log": 80}, "housing": {"occupiedBeds": 25, "totalBeds": 30, "homeless": 0}, "employment": {"assigned": 18, "vacancies": 22, "unemployed": 2}, "wellbeing": {"average": 12.3, "miserable": 0, "critical": 1}, "dc": {"x": 120, "y": 140, "z": 2, "orientation": "south", "entranceX": 120, "entranceY": 139}}],
      "trees": {"markedGrown": 5, "markedSeedling": 2, "unmarkedGrown": 120, "species": [{"name": "Pine", "markedGrown": 3, "unmarkedGrown": 80, "seedling": 12}]},
      "crops": {"ready": 15, "growing": 30, "species": [{"name": "Kohlrabi", "ready": 10, "growing": 20}]},
      "wellbeing": {"average": 12.3, "miserable": 0, "critical": 1, "categories": [{"group": "SocialLife", "current": 0.5, "max": 2.0}, {"group": "Fun", "current": 1.2, "max": 3.0}]},
      "science": 450,
      "alerts": {"unstaffed": 3, "unpowered": 1, "unreachable": 0},
      "buildings": {"housing": 5, "wood": 3, "storage": 2, "power": 1, "food": 2, "water": 1},
      "treeClusters": [{"x": 125, "y": 145, "z": 2, "grown": 15, "total": 22, "species": {"Pine": 12, "Birch": 5}}],
      "foodClusters": [{"x": 115, "y": 135, "z": 2, "grown": 8, "total": 12, "species": {"BlueberryBush": 8, "Dandelion": 4}}]
    }
    ```

#### Response (format=toon)

Flat key-value pairs including `settlement`, `faction`, `day`, `dayProgress`, `speed`, `cycle`, `cycleDay`, `isHazardous`, `tempDays`, `hazardDays`, `markedGrown`, `markedSeedling`, `unmarkedGrown`, `cropReady`, `cropGrowing`, `adults`, `children`, `bots`, resource stocks (e.g. `Water`, `Log`), `foodDays`, `waterDays`, `logDays`, `plankDays`, `gearDays`, `beds`, `homeless`, `workers`, `unemployed`, `wellbeing`, `miserable`, `critical`, `science`, `alerts`, building role counts, `treeClusters`, `foodClusters`.

---

## Agent

Built-in interactive agent control. The in-game widget is the primary surface for these actions, but the same shared agent is also available over HTTP.

### GET /api/agent/status

Current built-in agent status.

#### Response

| Field | Type | Description |
|-------|------|-------------|
| status | string | `idle`, `gatheringstate`, `interactive`, `done`, or `error` |
| binary | string | Current agent binary, usually `claude`, `codex`, or a custom command |
| model | string | Current model name, or `""` if default |
| goal | string | Current goal text |
| currentCmd | string | Internal progress text while gathering state, otherwise `""` |
| lastError | string | Last launch/runtime error, if any |

```json
{
  "status": "interactive",
  "binary": "codex",
  "model": "gpt-5.4",
  "goal": "reach 50 beavers with 77 well-being",
  "currentCmd": "",
  "lastError": ""
}
```

### POST /api/agent/start

Start the built-in interactive agent. The C# launcher shells out to `tbot agent run`, which fetches fresh colony state via `TimberbotClient.brain`, merges `tbot.agent_prompts.timberbot` (or the user's edited copy under `~/.config/timberbot/agent_prompts/`) with that state into a per-launch instructions file, and dispatches to the chosen backend (`claude`, `codex`, `opencode`, or `custom`).

#### Body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| binary | string | no | `claude` | CLI binary to launch |
| model | string | no | binary default | Model name passed via `--model` |
| effort | string | no | binary default | Reasoning effort passed via `--effort` |
| timeout | int | no | `120` | Timeout in seconds for the `brain` gather step |
| goal | string | no | built-in default | Goal text appended to the startup message |
| command | string | no | `null` | Custom launch template when `binary` is `custom` |

```json
{
  "binary": "codex",
  "model": "gpt-5.4",
  "effort": "medium",
  "goal": "reach 50 beavers with 77 well-being"
}
```

#### Response

```json
{"status": "started", "binary": "codex"}
```

Possible errors:
- `agent_busy` if the agent is already gathering state or running interactively

### POST /api/agent/stop

Stop the current built-in agent session.

#### Response

```json
{"status": "stopping"}
```

Possible errors:
- `agent_not_running` if there is no active session to stop

---

### GET /api/time

Current day number and progress.

**CLI:** `tbot time`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| dayNumber | int | Current day number |
| dayProgress | float | Progress through current day (0.0-1.0) |
| partialDayNumber | float | Fractional day number |

```json
{"dayNumber": 42, "dayProgress": 0.65, "partialDayNumber": 42.65}
```

---

### GET /api/weather

Current weather cycle and drought info.

**CLI:** `tbot weather`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| cycle | int | Current cycle number |
| cycleDay | int | Day within current cycle |
| isHazardous | bool | Currently in drought/badtide |
| temperateWeatherDuration | int | Days of temperate weather this cycle |
| hazardousWeatherDuration | int | Days of hazardous weather this cycle |
| cycleLengthInDays | int | Total cycle length |

```json
{
  "cycle": 3,
  "cycleDay": 5,
  "isHazardous": false,
  "temperateWeatherDuration": 12,
  "hazardousWeatherDuration": 6,
  "cycleLengthInDays": 18
}
```

---

### GET /api/power

Power networks. Groups all powered buildings by their connected network. Buildings sharing a power network have the same supply and demand values.

**CLI:** `tbot power`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| id | int | Network identity hash |
| supply | float | Current power supply (from generators with workers) |
| demand | float | Total power demand from consumers |
| buildings | array | Buildings on this network |
| buildings[].name | string | Building name |
| buildings[].id | int | Instance ID |
| buildings[].isGenerator | bool | Power source (wheel, engine) |
| buildings[].nominalOutput | float | Max power output capacity |
| buildings[].nominalInput | float | Power draw when running |

Networks with `supply < demand` are underpowered — powered buildings run intermittently. Isolated buildings (no power chain) appear as single-building networks with `supply: 0`.

---

### GET /api/speed

Current game speed. Answered on listener thread (works when paused).

**CLI:** `tbot speed`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| speed | int | 0=pause, 1=normal, 2=fast, 3=fastest |

```json
{"speed": 1}
```

---

### POST /api/speed

Set game speed.

**CLI:** `tbot set_speed speed:2`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| speed | int | yes | 0=pause, 1=normal, 2=fast, 3=fastest |

```json
{"speed": 2}
```

#### Response (success)

```json
{"speed": 2}
```

#### Response (error)

```json
{"error": "invalid_param: speed must be 0-3 (0=pause, 1=normal, 2=fast, 3=fastest)", "got": 5}
```

---

### GET /api/workhours

Current work schedule.

**CLI:** `tbot workhours`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| endHours | float | Hour when work ends (1-24) |
| areWorkingHours | bool | Whether beavers are currently working |

```json
{"endHours": 16.0, "areWorkingHours": true}
```

---

### POST /api/workhours

Set when beavers stop working.

**CLI:** `tbot set_workhours end_hours:14`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| endHours | int | yes | When work ends (1-24) |

#### Response (success)

```json
{"endHours": 14}
```

#### Response (error)

```json
{"error": "invalid_param: endHours must be 1-24 (hour when work stops, default 18)", "got": 30}
```

---

### GET /api/notifications

Game event history.

**CLI:** `tbot notifications`

#### Response

Array of notification objects.

| Field | Type | Description |
|-------|------|-------------|
| subject | string | Event title |
| description | string | Event details |
| cycle | int | Cycle when event occurred |
| cycleDay | int | Day within cycle |

```json
[
  {"subject": "Drought ended", "description": "The drought is over.", "cycle": 2, "cycleDay": 18}
]
```

---

### GET /api/alerts

Buildings with problems: unstaffed, unpowered, unreachable, or status messages.

**CLI:** `tbot alerts`

#### Response

Array of alert objects.

| Field | Type | Description |
|-------|------|-------------|
| type | string | `"unstaffed"`, `"unpowered"`, `"unreachable"`, or `"status"` |
| id | int | Building instance ID |
| name | string | Building name |
| workers | string | (unstaffed only) `"assigned/desired"` |
| status | string | (status only) Status description |

```json
[
  {"type": "unstaffed", "id": 12340, "name": "LumberjackFlag", "workers": "0/1"},
  {"type": "unpowered", "id": 12350, "name": "Gristmill"},
  {"type": "unreachable", "id": 12360, "name": "SmallWarehouse"}
]
```

---

## Districts & Resources

### GET /api/population

Beaver and bot counts per district.

**CLI:** `tbot population`

#### Response

Array of district population objects.

| Field | Type | Description |
|-------|------|-------------|
| district | string | District name |
| adults | int | Adult beaver count |
| children | int | Child beaver count |
| bots | int | Mechanical beaver count |

```json
[
  {"district": "District 1", "adults": 20, "children": 5, "bots": 2}
]
```

---

### GET /api/resources

Resource stocks per district.

**CLI:** `tbot resources` | `tbot --json resources`

#### Response (format=toon)

Flat array of `{district, good, available, all}` per resource with stock > 0.

```json
[
  {"district": "District 1", "good": "Water", "available": 150, "all": 200},
  {"district": "District 1", "good": "Log", "available": 80, "all": 80}
]
```

#### Response (format=json)

Keyed by district name, each containing resource objects with `{available, all}`.

```json
{
  "District 1": {
    "Water": {"available": 150, "all": 200},
    "Log": {"available": 80, "all": 80}
  }
}
```

---

### GET /api/districts

Districts with population and resource data combined.

**CLI:** `tbot districts` | `tbot --json districts`

#### Response (format=json)

| Field | Type | Description |
|-------|------|-------------|
| name | string | District name |
| population | object | `{adults, children, bots}` |
| resources | object | Keyed by good name: `{available, all}` |

```json
[
  {
    "name": "District 1",
    "population": {"adults": 20, "children": 5, "bots": 2},
    "resources": {"Water": {"available": 150, "all": 200}, "Log": {"available": 80, "all": 80}}
  }
]
```

#### Response (format=toon)

Flat rows with `name`, `adults`, `children`, `bots`, plus one key per resource with stock > 0.

---

### GET /api/distribution

Import/export settings per good per district.

**CLI:** `tbot distribution`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| district | string | District name |
| goods | array | Per-good settings |
| goods[].good | string | Good name |
| goods[].importOption | string | `"None"`, `"Allowed"`, or `"Forced"` |
| goods[].exportThreshold | float | Export when stock exceeds this |

```json
[
  {
    "district": "District 1",
    "goods": [
      {"good": "Water", "importOption": "Allowed", "exportThreshold": 50.0},
      {"good": "Log", "importOption": "None", "exportThreshold": 0.0}
    ]
  }
]
```

---

### POST /api/distribution

Set import/export for a specific good in a district.

**CLI:** `tbot set_distribution district:"District 1" good:Log import_option:Forced export_threshold:50`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| district | string | yes | District name |
| good | string | yes | Good name (e.g. `"Log"`) |
| import | string | no | `"None"`, `"Allowed"`, or `"Forced"` |
| exportThreshold | int | no | Export threshold (-1 to skip) |

#### Response (success)

```json
{"district": "District 1", "good": "Log", "importOption": "Forced", "exportThreshold": 50}
```

#### Response (error)

```json
{"error": "not_found: district does not exist", "district": "Bad Name", "districts": ["District 1", "District 2"]}
```

---

### POST /api/district/migrate

Move adult beavers between districts.

**CLI:** `tbot migrate from_district:"District 1" to_district:"District 2" count:3`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| from | string | yes | Source district name |
| to | string | yes | Destination district name |
| count | int | yes | Number of adults to move |

#### Response (success)

```json
{"from": "District 1", "to": "District 2", "migrated": 3}
```

#### Response (error)

```json
{"error": "not_found: source district does not exist", "from": "Bad Name", "districts": ["District 1", "District 2"]}
```

```json
{"error": "no_population: no beavers available to migrate in this district", "from": "District 1", "available": 0, "requested": 3}
```

---

## Entities

### GET /api/buildings

All placed buildings with state.

**CLI:** `tbot buildings` | `tbot --json buildings` | `tbot buildings name:Pump` | `tbot buildings x:120 y:140 radius:20`

Supports server-side pagination (`?limit=10&offset=20`) and filtering (`?name=Farm`, `?x=120&y=140&radius=20`). All filters are available as CLI params too. See [Pagination](#pagination) above.

#### Response (format=json)

Each building includes all applicable fields (absent fields mean the component doesn't exist on that building):

| Field | Type | Description |
|-------|------|-------------|
| id | int | Deterministic stable hash |
| name | string | Faction-qualified building name (for example `FarmHouse.IronTeeth`) |
| x, y, z | int | Origin coordinates |
| orientation | string | `"south"`, `"west"`, `"north"`, or `"east"` |
| finished | bool | Construction complete |
| pausable | bool | Can be paused |
| paused | bool | Currently paused |
| floodgate | bool | Is a floodgate |
| height | float | (floodgate) Current gate height |
| maxHeight | float | (floodgate) Maximum gate height |
| constructionPriority | string | Priority while building |
| workplacePriority | string | Priority when finished |
| maxWorkers | int | Maximum worker slots |
| desiredWorkers | int | Desired worker count |
| assignedWorkers | int | Currently assigned workers |
| reachable | bool | Connected to district via paths |
| powered | bool | Has power (mechanical buildings) |
| isGenerator | bool | Generates power |
| isConsumer | bool | Consumes power |
| nominalPowerInput | int | Rated power input |
| nominalPowerOutput | int | Rated power output |
| powerDemand | int | Current grid power demand |
| powerSupply | int | Current grid power supply |
| buildProgress | float | Construction time progress (0-1) |
| materialProgress | float | Material delivery progress (0-1) |
| hasMaterials | bool | Has materials to resume |
| inventory | object | Goods in stock: `{"Log": 5, "Plank": 3}` |
| statuses | array | Active status messages |
| isWonder | bool | Is a monument/wonder |
| wonderActive | bool | Wonder is activated |
| dwellers | int | Current residents |
| maxDwellers | int | Max residents |
| isClutch | bool | Has a clutch mechanism |
| clutchEngaged | bool | Clutch is engaged |
| stock | int | Total items in all inventories |
| capacity | int | Total inventory capacity |
| storageMode | string | `"accept"`, `"obtain"`, `"supply"`, or `"empty"` (storage buildings only, empty string otherwise) |
| allowedGood | string | Selected good for storage (empty if none selected) |
| recipes | array | Available recipe IDs for manufactories |
| currentRecipe | string | Active recipe ID (empty if none) |
| needsNutrients | bool | Breeding pod needs food delivered |
| nutrients | int | Breeding pod nutrient count |
| entranceX, entranceY, entranceZ | int | Entrance block on the building |
| automation | object | (optional) Real-time automation state & config |
| automation.type | string | Specific sensor/logic component type (e.g. `"Memory"`) |
| automation.state | string | Current signal: `"On"`, `"Off"`, or `"Error"` |
| automation.config | object | Thresholds & operational modes specific to the component type |
| automation.inputs | array | Active incoming logical links: `[{"key": "a", "id": 123, "name": "Sensor"}]` |
| automation.outputs | array | Active destination targets: `[{"id": 456, "name": "WaterPump"}]` |

```json
[
  {
    "id": 12340,
    "name": "LumberjackFlag",
    "x": 120, "y": 130, "z": 2,
    "orientation": "south",
    "finished": true,
    "pausable": true,
    "paused": false,
    "workplacePriority": "Normal",
    "maxWorkers": 1,
    "desiredWorkers": 1,
    "assignedWorkers": 1,
    "reachable": true,
    "entranceX": 120, "entranceY": 129, "entranceZ": 2,
    "automation": {
      "hasAutomator": 1,
      "isTransmitter": 0,
      "state": "Off",
      "isAutomated": 1,
      "type": "Automatable",
      "config": {},
      "inputs": [{"key": "input", "id": 42, "name": "DepthSensor"}],
      "outputs": []
    }
  }
]
```

#### Response (format=toon)

Flat rows with: `id`, `name`, `x`, `y`, `z`, `orientation`, `finished`, `paused`, `priority`, `workers` (as `"assigned/desired"`).

---

### GET /api/trees

All trees (alive and dead).

**CLI:** `tbot trees`

Supports server-side pagination (`?limit=10`) and filtering (`?name=Pine`). See [Pagination](#pagination) above.

#### Response

| Field | Type | Description |
|-------|------|-------------|
| id | int | Instance ID |
| name | string | Tree species name |
| x, y, z | int | Coordinates |
| alive | bool | Not a dead stump |
| marked | bool | Marked for cutting |
| grown | bool | Fully grown |
| growth | float | Growth progress (0.0-1.0) |

```json
[
  {"id": 45600, "name": "Pine", "x": 115, "y": 140, "z": 2, "alive": true, "marked": false, "grown": true, "growth": 1.0}
]
```

---

### GET /api/crops

All crops (Kohlrabi, Soybean, Corn, etc) with growth status.

**CLI:** `tbot crops`

#### Response

Same fields as `/api/trees`. Filters to crop species only.

```json
[
  {"id": 78900, "name": "Kohlrabi", "x": 125, "y": 141, "z": 2, "alive": true, "marked": false, "grown": false, "growth": 0.45}
]
```

---

### GET /api/gatherables

Berry bushes and other gatherable resources.

**CLI:** `tbot gatherables`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| id | int | Instance ID |
| name | string | Resource name |
| x, y, z | int | Coordinates |
| alive | bool | Not dead |

```json
[
  {"id": 45700, "name": "BlueberryBush", "x": 110, "y": 135, "z": 2, "alive": true}
]
```

---

### GET /api/beavers

All beavers and bots with wellbeing and needs.

**CLI:** `tbot beavers` | `tbot --json beavers`

Supports server-side pagination (`?limit=10`) and filtering (`?name=Bot`). See [Pagination](#pagination) above.

#### Detail modes

| Mode | Description |
|------|-------------|
| `detail=basic` (default) | Active needs only, compact |
| `detail=full` | All needs (active + inactive) with `group` category field |
| `id=<id>` | Single beaver/bot by ID, all needs with `group` field |

**CLI:** `tbot beavers detail:full` | `tbot beavers id:-12345`

Bots always show all 3 needs (Energy, ControlTower, Grease) regardless of detail mode.

#### Response (format=json)

| Field | Type | Description |
|-------|------|-------------|
| id | int | Instance ID |
| name | string | Beaver name |
| x, y, z | int | Grid position on the map |
| wellbeing | float | Wellbeing score |
| district | string | District name (e.g. "District 1") |
| needs | array | Per-need breakdown (see below) |
| needs[].id | string | Need name (Hunger, Thirst, Campfire, Scratcher, etc.) |
| needs[].points | float | Current points (0-1, higher = more satisfied) |
| needs[].wellbeing | int | Wellbeing contribution from this need |
| needs[].favorable | bool | Need is satisfied |
| needs[].critical | bool | Need is in critical state |
| needs[].group | string | Need category: BasicNeeds, Fun, Nutrition, Aesthetics, Awe, SocialLife, Boosts (detail=full only) |
| anyCritical | bool | Any need below warning threshold |
| lifeProgress | float | Age progress (0.0-1.0) |
| workplace | string | Assigned workplace name (empty if none) |
| isBot | bool | Mechanical beaver |
| carrying | string | (optional) Good being hauled (e.g. "Water", "Log") |
| carryAmount | int | (optional) Units being carried |
| liftingCapacity | int | Max carry capacity (detail=full only) |
| overburdened | bool | (optional, detail=full) Carrying heavy load, movement slowed |
| deterioration | float | (optional, bots only) Deterioration progress 0-1 (1 = fully degraded) |
| contaminated | bool | Contaminated by badwater |
| activity | string | Current status text (e.g. "Waiting for nutrients", "No available workers") |
| hasHome | bool | Has assigned dwelling |

**Bot needs:** Bots always show all 3 needs regardless of active state: `Energy` (charge from ChargingStation, drains 0.58/day), `ControlTower` (boost from ControlTower building, drains 72/day), `Grease` (from GreaseFactory, drains 0.2/day). Points range 0-1.

```json
[
  {
    "id": 78900,
    "name": "Bucky",
    "wellbeing": 14.2,
    "needs": [
      {"id": "Hunger", "points": 0.8, "wellbeing": 1, "favorable": true, "critical": false},
      {"id": "Thirst", "points": 0.6, "wellbeing": 1, "favorable": true, "critical": false}
    ],
    "anyCritical": false,
    "lifeProgress": 0.35,
    "workplace": "LumberjackFlag",
    "isBot": false,
    "contaminated": false,
    "hasHome": true
  }
]
```

#### Response (format=toon)

Flat rows with: `id`, `name`, `wellbeing`, `tier` (ecstatic/happy/okay/unhappy/miserable), `isBot`, `workplace`, `critical` (need names joined with `+`).

Tier thresholds: >= 16 ecstatic, >= 12 happy, >= 8 okay, >= 4 unhappy, < 4 miserable.

---

### GET /api/prefabs

All building templates with dimensions.

**CLI:** `tbot prefabs`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| name | string | Prefab name (e.g. `"LumberjackFlag.IronTeeth"`) |
| sizeX | int | Width |
| sizeY | int | Depth |
| sizeZ | int | Height |
| scienceCost | int | Science points to unlock (omitted if 0) |
| unlocked | bool | Whether unlocked (omitted if no science cost) |
| cost | array | Material cost: `[{"good": "Log", "amount": 2}]` |

```json
[
  {
    "name": "FarmHouse.IronTeeth", "sizeX": 2, "sizeY": 2, "sizeZ": 2,
    "cost": [{"good": "Log", "amount": 4}, {"good": "Plank", "amount": 2}]
  }
]
```

---

### GET /api/tree_clusters

Top 5 clusters of grown trees by density.

**CLI:** `tbot tree_clusters`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| x | int | Cluster center X |
| y | int | Cluster center Y |
| z | int | Cluster Z level |
| grown | int | Fully grown trees in cluster |
| total | int | Total trees in cluster |
| species | object | Count per species: `{"Pine": 12, "Birch": 5}` |

```json
[
  {"x": 125, "y": 145, "z": 2, "grown": 15, "total": 22, "species": {"Pine": 12, "Birch": 5}}
]
```

---

### GET /api/food_clusters

Top 5 clusters of gatherable food (berries, bushes) by density. Excludes tree species.

**CLI:** `tbot food_clusters`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| x | int | Cluster center X |
| y | int | Cluster center Y |
| z | int | Cluster Z level |
| grown | int | Fully grown gatherables in cluster |
| total | int | Total gatherables in cluster |
| species | object | Count per species: `{"BlueberryBush": 8, "Dandelion": 4}` |

```json
[
  {"x": 115, "y": 135, "z": 2, "grown": 8, "total": 12, "species": {"BlueberryBush": 8, "Dandelion": 4}}
]
```

---

### GET /api/settlement

Settlement name for the current save game. Answered on listener thread (works even when paused).

**CLI:** used internally by `brain` for per-settlement memory folders.

#### Response

| Field | Type | Description |
|-------|------|-------------|
| name | string | Settlement / save name |

```json
{"name": "My Colony"}
```

---

## Map & Terrain

### GET /api/tiles

Terrain, water, occupants, and contamination for a rectangular region.

**CLI:** `tbot tiles x1:100 y1:100 x2:110 y2:110`

#### Query Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x1 | int | yes | Region min X |
| y1 | int | yes | Region min Y |
| x2 | int | yes | Region max X |
| y2 | int | yes | Region max Y |

#### Response

| Field | Type | Description |
|-------|------|-------------|
| mapSize | object | `{x, y, z}` total map dimensions |
| region | object | `{x1, y1, x2, y2}` clamped region |
| tiles | array | Per-tile data |
| tiles[].x, y | int | Tile coordinates |
| tiles[].terrain | int | Terrain height |
| tiles[].water | float | Water depth at tile |
| tiles[].badwater | float | (optional) Water contamination 0-1 |
| tiles[].occupants | array/string | (optional) json: `[{name, z}, ...]` array. toon: flat string `"Path:2/Stairs:3"` |
| tiles[].entrance | bool | (optional) Is an entrance tile |
| tiles[].seedling | bool | (optional) Has a seedling |
| tiles[].dead | bool | (optional) Dead tree stump (buildable) |
| tiles[].contaminated | bool | (optional) Soil contamination |
| tiles[].moist | bool | (optional) Irrigated soil |

```json
{
  "mapSize": {"x": 256, "y": 256, "z": 22},
  "region": {"x1": 100, "y1": 100, "x2": 110, "y2": 110},
  "tiles": [
    {"x": 100, "y": 100, "terrain": 2, "water": 0.0},
    {"x": 100, "y": 101, "terrain": 2, "water": 1.5, "badwater": 0.3},
    {"x": 100, "y": 102, "terrain": 2, "water": 0.0, "occupants": [{"name": "Path", "z": 2}], "moist": true}
  ]
}
```

---

## Science

### GET /api/science

Science points and unlockable buildings.

**CLI:** `tbot science`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| points | int | Current science points |
| unlockables | array | Buildings that cost science |
| unlockables[].name | string | Building prefab name |
| unlockables[].cost | int | Science cost |
| unlockables[].unlocked | bool | Already unlocked |

```json
{
  "points": 450,
  "unlockables": [
    {"name": "Gristmill.IronTeeth", "cost": 200, "unlocked": true},
    {"name": "Engine.IronTeeth", "cost": 600, "unlocked": false}
  ]
}
```

---

## Wellbeing

### GET /api/wellbeing

Population wellbeing breakdown by category. Shows current vs max score for each need group across all beavers.

**CLI:** `tbot wellbeing`

#### Response

| Field | Type | Description |
|-------|------|-------------|
| beavers | int | Number of beavers counted |
| categories | array | Wellbeing categories |
| categories[].group | string | Category name (BasicNeeds, SocialLife, Fun, Nutrition, Aesthetics, Awe) |
| categories[].current | float | Average current wellbeing for this category |
| categories[].max | float | Average max possible wellbeing for this category |
| categories[].needs | array | Individual needs in this category |
| categories[].needs[].id | string | Need/building name |
| categories[].needs[].favorableWellbeing | float | Wellbeing bonus when satisfied |
| categories[].needs[].unfavorableWellbeing | float | Wellbeing penalty when unmet |

```json
{
  "beavers": 42,
  "categories": [
    {
      "group": "SocialLife",
      "current": 0,
      "max": 2,
      "needs": [
        {"id": "Campfire", "favorableWellbeing": 1, "unfavorableWellbeing": 0},
        {"id": "RooftopTerrace", "favorableWellbeing": 1, "unfavorableWellbeing": 0}
      ]
    }
  ]
}
```

---

### POST /api/science/unlock

Unlock a building using science points. Matches the exact UI flow (cost deduction + events + UI refresh).

**CLI:** `tbot unlock_building building:"Engine.IronTeeth"`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| building | string | yes | Prefab name from `GET /api/science` |

#### Response (success)

```json
{"building": "Engine.IronTeeth", "unlocked": true, "remaining": 250}
```

#### Response (already unlocked)

```json
{"building": "Engine.IronTeeth", "unlocked": true, "remaining": 450, "note": "already unlocked"}
```

#### Response (error, insufficient points)

```json
{"error": "insufficient_science: not enough science points to unlock", "building": "Engine.IronTeeth", "scienceCost": 600, "currentPoints": 450}
```

#### Response (error, not found)

```json
{"error": "not_found: building not in toolbar. use prefabs to list all building names", "building": "BadName"}
```

---

## Building Actions

### POST /api/building/pause

Pause or unpause a building.

**CLI:** `tbot pause_building id:12340` | `tbot unpause_building id:12340`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| paused | bool | yes | `true` to pause, `false` to unpause |

#### Response (success)

```json
{"id": 12340, "name": "LumberjackFlag", "paused": true}
```

#### Response (error)

```json
{"error": "not_found: no entity with this id", "id": 99999}
```

```json
{"error": "invalid_type: this building cannot be paused", "id": 12340, "name": "Levee"}
```

---

### POST /api/building/clutch

Engage or disengage a clutch on a building.

**CLI:** `tbot set_clutch id:12340 engaged:true`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| engaged | bool | yes | `true` to engage, `false` to disengage |

#### Response

```json
{"id": 12340, "name": "GravityBattery", "engaged": true}
```

---

### POST /api/building/demolish

Remove a building from the world.

**CLI:** `tbot demolish_building id:12340`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |

#### Response (success)

```json
{"id": 12340, "name": "LumberjackFlag", "demolished": true}
```

#### Response (error)

```json
{"error": "not_found: no entity with this id", "id": 99999}
```

---

### POST /api/crop/demolish

Remove a planted crop entity from the world.

**CLI:** `tbot demolish_crop id:12340`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Crop instance ID |

#### Response (success)

```json
{"id": 12340, "name": "Kohlrabi", "demolished": true}
```

#### Response (error)

```json
{"error": "not_found: no entity with this id", "id": 99999}
```

---

### POST /api/building/place

Place a building in the world. Validates all tiles before placing: occupancy, terrain height, water, unlock status, underground clipping. Coordinates refer to the bottom-left corner regardless of orientation.

**CLI:** `tbot place_building prefab:Path x:120 y:130 z:2 orientation:south`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| prefab | string | yes | Prefab name from `GET /api/prefabs` |
| x | int | yes | Bottom-left X |
| y | int | yes | Bottom-left Y |
| z | int | yes | Terrain height (must match) |
| orientation | string | yes | south, west, north, east |

!!! danger "Ghost buildings"
    Failed placements may create invisible entities. See [Known Issues](#known-issues).

#### Response (success)

```json
{"id": 12340, "name": "LumberjackFlag", "x": 120, "y": 130, "z": 2, "orientation": 0}
```

#### Response (error)

```json
{"error": "invalid_prefab: 'BadName' not found. Similar: LumberjackFlag.IronTeeth, LumberjackFlag.Folktails", "prefab": "BadName"}
```

```json
{"error": "not_unlocked: use science/unlock first", "x": 120, "y": 130, "z": 2, "prefab": "Engine.IronTeeth", "scienceCost": 600, "currentPoints": 450}
```

```json
{"error": "occupied by Path at (120,130,2). demolish it or try a different location", "x": 120, "y": 130, "z": 2, "prefab": "LumberjackFlag.IronTeeth"}
```

---

### POST /api/placement/find

Find valid placements for a building within a rectangular area. Returns at most 10 results. Water buildings sort by waterDepth first (deepest water preferred). Others sort by: non-flooded > reachable > pathAccess > nearPower.

**CLI:** `tbot find_placement prefab:LumberjackFlag.IronTeeth x1:110 y1:125 x2:130 y2:145`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| prefab | string | yes | Prefab name |
| x1 | int | yes | Search area min X |
| y1 | int | yes | Search area min Y |
| x2 | int | yes | Search area max X |
| y2 | int | yes | Search area max Y |

#### Response (success)

| Field | Type | Description |
|-------|------|-------------|
| prefab | string | Requested prefab |
| sizeX | int | Building width |
| sizeY | int | Building depth |
| placements | array | Valid positions (max 10) |
| placements[].x, y, z | int | Bottom-left coordinates |
| placements[].orientation | string | Best orientation name |
| placements[].entranceX | int | Doorstep tile X (where path must connect) |
| placements[].entranceY | int | Doorstep tile Y (where path must connect) |
| placements[].pathAccess | int | 1 if doorstep tile has a path, 0 otherwise |
| placements[].reachable | int | 1 if connected to district road network, 0 otherwise |
| placements[].nearPower | int | 1 if adjacent to power building, 0 otherwise |
| placements[].flooded | int | 1 if water on ground tiles. Flooded buildings are non-functional |
| placements[].waterDepth | float | Water depth at intake tile (water buildings only) |
| placements[].distance | float | Path distance from DC entrance via flow field (-1 if unreachable, lower = closer) |

Water buildings (pumps) sort by: waterDepth (deepest first). Others sort by: non-flooded > reachable > distance (closer first) > pathAccess > nearPower.

**JSON format** (nested wrapper with prefab metadata):
```json
{
  "prefab": "LumberjackFlag.IronTeeth",
  "sizeX": 2, "sizeY": 2,
  "placements": [
    {"x": 120, "y": 130, "z": 2, "orientation": "south", "entranceX": 120, "entranceY": 129, "pathAccess": 1, "reachable": 1, "distance": 12.0, "nearPower": 0, "flooded": 0}
  ]
}
```

**TOON format** (flat array, same keys, no wrapper):
```json
[{"x": 120, "y": 130, "z": 2, "orientation": "south", "entranceX": 120, "entranceY": 129, "pathAccess": 1, "reachable": 1, "distance": 12.0, "nearPower": 0, "flooded": 0}]
```

#### Response (error)

```json
{"error": "invalid_prefab: 'BadName' not found. Similar: LumberjackFlag.IronTeeth, LumberjackFlag.Folktails", "prefab": "BadName"}
```

---

### POST /api/building/floodgate

Set floodgate water gate height. Value is clamped to max.

**CLI:** `tbot set_floodgate id:12340 height:1.5`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| height | float | yes | Desired gate height (clamped to 0-max) |

#### Response (success)

```json
{"id": 12340, "name": "Floodgate", "height": 1.5, "maxHeight": 3.0}
```

#### Response (error)

```json
{"error": "invalid_type: not a floodgate. use buildings name:Floodgate to find floodgates", "id": 12340, "name": "LumberjackFlag"}
```

---

### POST /api/building/priority

Set construction or workplace priority.

**CLI:** `tbot set_priority id:12340 priority:VeryHigh`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| priority | string | yes | `"VeryLow"`, `"Normal"`, or `"VeryHigh"` |
| type | string | no | `"construction"` or `"workplace"`. If empty, sets whichever exists |

#### Response (success)

```json
{"id": 12340, "name": "LumberjackFlag", "workplacePriority": "VeryHigh"}
```

```json
{"id": 12340, "name": "LumberjackFlag", "constructionPriority": "VeryHigh"}
```

#### Response (error)

```json
{"error": "invalid_param: priority must be one of: VeryLow, Low, Normal, High, VeryHigh", "got": "Bad"}
```

---

### POST /api/building/workers

Set desired worker count for a workplace.

**CLI:** `tbot set_workers id:12340 count:2`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| count | int | yes | Desired workers (clamped to 0-maxWorkers) |

#### Response (success)

```json
{"id": 12340, "name": "LumberjackFlag", "desiredWorkers": 2, "maxWorkers": 3, "assignedWorkers": 1}
```

#### Response (error)

```json
{"error": "invalid_type: not a workplace. only staffed buildings (lumberjacks, farms, etc) have workers", "id": 12340, "name": "Path"}
```

---

### POST /api/building/hauling

Prioritize hauling deliveries to a building.

**CLI:** `tbot set_haul_priority id:12340 prioritized:true`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| prioritized | bool | yes | `true` to prioritize, `false` to clear |

#### Response (success)

```json
{"id": 12340, "name": "SmallWarehouse", "haulPrioritized": true}
```

#### Response (error)

```json
{"error": "invalid_type: no haul priority. only buildings with inventories support haul priority", "id": 12340, "name": "Path"}
```

---

### POST /api/building/recipe

Set which recipe a manufactory produces.

**CLI:** `tbot set_recipe id:12340 recipe:PlankRecipe`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| recipe | string | yes | Recipe ID, or `"none"` to clear |

#### Response (success)

```json
{"id": 12340, "name": "LumberMill", "recipe": "PlankRecipe"}
```

```json
{"id": 12340, "name": "LumberMill", "recipe": "none"}
```

#### Response (error, recipe not found)

```json
{"error": "not_found: recipe does not exist for this building", "recipeId": "BadRecipe", "available": ["PlankRecipe", "TreatedPlankRecipe"]}
```

---

### POST /api/building/farmhouse

Prioritize planting or harvesting for a farmhouse.

**CLI:** `tbot set_farmhouse_action id:12340 action:planting`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| action | string | yes | `"planting"` or `"harvesting"` (harvesting = default behavior) |

#### Response (success)

```json
{"id": 12340, "name": "FarmHouse", "action": "planting"}
```

```json
{"id": 12340, "name": "FarmHouse", "action": "default"}
```

#### Response (error)

```json
{"error": "invalid_type: not a farmhouse. use buildings name:FarmHouse to find farmhouses", "id": 12340, "name": "LumberjackFlag"}
```

```json
{"error": "invalid_param: action must be 'planting' or 'harvesting'", "got": "bad"}
```

---

### POST /api/building/plantable

Prioritize which tree/resource type a forester plants.

**CLI:** `tbot set_plantable_priority id:12340 plantable:Pine`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| plantable | string | yes | Plantable template name, or `"none"` to clear |

#### Response (success)

```json
{"id": 12340, "name": "Forester", "prioritized": "Pine"}
```

```json
{"id": 12340, "name": "Forester", "prioritized": "none"}
```

#### Response (error, not found)

```json
{"error": "not_found: plantable not in this building's list", "plantableName": "BadTree", "available": ["Pine", "Birch", "Oak"]}
```

---

### POST /api/building/storage

Set storage mode and/or allowed good on any storage building (piles, warehouses, tanks). Set either or both fields in one call.

**CLI:** `tbot set_storage id:12340 good:Water mode:obtain`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building instance ID |
| good | string | no | Good name (e.g. `"Water"`, `"Plank"`) or `"none"` to clear |
| mode | string | no | `"accept"` (default), `"obtain"`, `"supply"`, or `"empty"` |

**Modes:**
- `accept`: normal behavior, haulers bring/take goods naturally
- `obtain`: haulers actively fetch this good from other stockpiles
- `supply`: haulers actively take goods from here to other stockpiles
- `empty`: haulers remove all goods, building stops accepting

#### Response (success)

```json
{"id": 12340, "name": "SmallTank.IronTeeth", "good": "Water", "mode": "obtain"}
```

#### Response (error)

```json
{"error": "invalid_type: not a storage building. piles, warehouses, and tanks have storage settings", "id": 12340, "name": "LumberjackFlag"}
```

```json
{"error": "invalid_param: mode must be accept, obtain, supply, or empty", "got": "badvalue"}
```

---

## Automation Actions

### POST /api/automation/link

Wire a sensor, relay, or other transmitter output to an automation input on a building.

**CLI:** `tbot link source_id:42 target_id:44 input:a`

#### Request Body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| sourceId | int | yes | | Transmitter entity ID |
| targetId | int | yes | | Target building entity ID |
| input | string | no | "a" | Which input: "a", "b", or "reset" (for Relay/Memory only) |

```json
{"sourceId": 42, "targetId": 44, "input": "a"}
```

#### Response (success)

```json
{"id": 44, "name": "FlourMill", "input": "a", "sourceId": 42, "sourceName": "DepthSensor", "connected": true}
```

For Automatable targets (single-input buildings), the `input` field always returns `"a"` regardless of what was passed, since these buildings have only one input.

#### Errors

| Error | Condition |
|-------|-----------|
| `invalid_param: input must be a or b for Relay` | Input is not "a" or "b" for a Relay target |
| `invalid_param: input must be a, b, or reset for Memory` | Input is not valid for a Memory target |
| `invalid_param: Relay mode X does not use input B` | Linking input B to a Relay in Not or Passthrough mode. Change mode first with `configure_automation property:mode` |
| `invalid_param: Memory mode X does not use input B` | Linking input B to a Memory in SetReset or Toggle mode. Change mode first with `configure_automation property:mode` |
| `invalid_type: source is not a transmitter` | Source entity has no Automator or it's not a transmitter |
| `invalid_type: target cannot accept automation input` | Target has no Automatable, Relay, or Memory component |

---

### POST /api/automation/unlink

Disconnect an automation input on a building.

**CLI:** `tbot unlink id:44 input:a`

#### Request Body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| id | int | yes | | Target building entity ID |
| input | string | no | "a" | Which input: "a", "b", or "reset" (for Relay/Memory only) |

```json
{"id": 44, "input": "a"}
```

#### Response (success)

```json
{"id": 44, "name": "FlourMill", "input": "a", "connected": false}
```

For Automatable targets (single-input buildings), the `input` field always returns `"a"`.

#### Errors

| Error | Condition |
|-------|-----------|
| `invalid_param: input must be a or b for Relay` | Input is not "a" or "b" for a Relay target |
| `invalid_param: input must be a, b, or reset for Memory` | Input is not valid for a Memory target |
| `invalid_param: Relay mode X does not use input B` | Unlinking input B from a Relay in Not or Passthrough mode |
| `invalid_param: Memory mode X does not use input B` | Unlinking input B from a Memory in SetReset or Toggle mode |
| `invalid_type: target has no automation input` | Target has no Automatable, Relay, or Memory component |

---

### POST /api/automation/configure

Configure a property on an automation component (sensor threshold, relay mode, lever state, etc.).

**CLI:** `tbot configure_automation id:42 property:threshold value:50`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Automation component entity ID |
| property | string | yes | Property name to set |
| value | string | yes | New value |

```json
{"id": 42, "property": "threshold", "value": "50"}
```

**Properties by component type:**

| Component | Properties |
|-----------|------------|
| DepthSensor / ContaminationSensor / FlowSensor | `threshold` (float), `mode` (Equal/NotEqual/Greater/GreaterOrEqual/Less/LessOrEqual) |
| ResourceCounter | `goodId` (string), `threshold` (int), `fillRateThreshold` (float), `mode` (StockLevel/FillRate), `comparisonMode` (NumericComparisonMode), `includeInputs` (bool) |
| PopulationCounter | `threshold` (int), `mode`, `comparisonMode`, `globalMode` (bool), `countBeavers` (bool), `countBots` (bool) |
| PowerMeter | `mode` (Power/Percent), `comparisonMode`, `intThreshold` (int), `percentThreshold` (float) |
| Relay | `mode` (Not/And/Or/Xor/Passthrough) |
| Memory | `mode` (SetReset/Toggle/Latch/FlipFlop) |
| Chronometer | `startTime` (float), `endTime` (float), `mode` (TimeRange/WorkingHours/NonWorkingHours) |
| Lever | `springReturn` (bool), `pinned` (bool) |

#### Response (success)

```json
{"id": 42, "name": "DepthSensor", "property": "threshold", "value": 50, "automationType": "DepthSensor"}
```

**Note:** The `id` field in the response matches the `id` request parameter.

---

## Area Actions

### POST /api/cutting/area

Mark or clear a rectangular area for tree cutting.

**CLI:** `tbot mark_trees x1:110 y1:130 x2:120 y2:140 z:2` | `tbot clear_trees x1:110 y1:130 x2:120 y2:140 z:2`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x1 | int | yes | Area min X |
| y1 | int | yes | Area min Y |
| x2 | int | yes | Area max X |
| y2 | int | yes | Area max Y |
| z | int | yes | Z level |
| marked | bool | yes | `true` to mark, `false` to clear |

#### Response

```json
{"x1": 110, "y1": 130, "x2": 120, "y2": 140, "z": 2, "marked": true, "tiles": 121}
```

---

### POST /api/planting/mark

Mark an area for crop planting. Validates tiles: skips occupied, water, and wrong terrain.

**CLI:** `tbot plant_crop x1:110 y1:130 x2:115 y2:135 z:2 crop:Carrot`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x1 | int | yes | Area min X |
| y1 | int | yes | Area min Y |
| x2 | int | yes | Area max X |
| y2 | int | yes | Area max Y |
| z | int | yes | Z level |
| crop | string | yes | Crop name (see [Crop Names](#crop-names)) |

#### Response

```json
{"x1": 110, "y1": 130, "x2": 115, "y2": 135, "z": 2, "crop": "Carrot", "planted": 28, "skipped": 8}
```

---

### POST /api/planting/clear

Clear planting marks from an area.

**CLI:** `tbot clear_planting x1:110 y1:130 x2:115 y2:135 z:2`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x1 | int | yes | Area min X |
| y1 | int | yes | Area min Y |
| x2 | int | yes | Area max X |
| y2 | int | yes | Area max Y |
| z | int | yes | Z level |

#### Response

```json
{"x1": 110, "y1": 130, "x2": 115, "y2": 135, "z": 2, "cleared": true, "tiles": 36}
```

---

### POST /api/planting/find

Find valid planting spots in an area or within a building's work range.

**CLI:** `tbot find_planting crop:Kohlrabi id:-514366` or `tbot find_planting crop:Kohlrabi x1:68 y1:128 x2:72 y2:132 z:2`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| crop | string | yes | Crop name (Kohlrabi, Pine, Birch, etc.) |
| id | int | no | Farmhouse/forester ID. returns spots within building range |
| x1, y1, x2, y2, z | int | no | Area scan (used when id is 0) |

#### Response

```json
{
  "crop": "Kohlrabi",
  "spots": [
    {"x": 120, "y": 135, "z": 2, "moist": true, "planted": false},
    {"x": 121, "y": 135, "z": 2, "moist": true, "planted": true}
  ]
}
```

---

### POST /api/building/range

Get the work range tiles for a building. Same green circle the player sees when selecting a farmhouse, lumberjack, forester, gatherer, scavenger, or district center.

**CLI:** `tbot building_range id:-514366`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | int | yes | Building ID |

#### Response

```json
{
  "id": -514366,
  "name": "FarmHouse",
  "tiles": 45,
  "moist": 32,
  "bounds": {"x1": 118, "y1": 130, "x2": 128, "y2": 145}
}
```

---

### POST /api/path/place

Route a path from point A to point B using A* pathfinding over a 3D surface graph. Routes around buildings, natural resources, ruins, water, and terrain obstacles. Handles diagonal routes, multi-z transitions with auto-stairs/platforms, and reuses existing paths/stairs/platforms.

**CLI:** `tbot place_path x1:120 y1:130 x2:150 y2:160`

#### Request Body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| x1 | int | yes | | Start X |
| y1 | int | yes | | Start Y |
| x2 | int | yes | | End X |
| y2 | int | yes | | End Y |
| style | string | no | "direct" | "direct" (shortest path) or "straight" (prefers straight lines) |
| sections | int | no | 0 | Stop after N stair/ramp crossings (0 = unlimited) |
| timings | bool | no | false | Include timing breakdown in response |

#### Response (success)

```json
{"placed": {"paths": 12, "stairs": 1}, "skipped": 0, "connectorEdgesInGrid": 4}
```

#### Response (partial. stopped by sections or error)

```json
{"placed": {"paths": 8, "stairs": 1}, "skipped": 0, "connectorEdgesInGrid": 4, "stoppedAt": "130,142", "errors": [{"error": "stair failed at (130,142,3)"}]}
```

#### Response (no route)

```json
{"placed": {"paths": 0}, "skipped": 0, "connectorEdgesInGrid": 0, "errors": [{"error": "A* found no route from (0,0) to (255,255). 0 connectors in graph"}]}
```

#### Response fields

| Field | Type | Description |
|-------|------|-------------|
| placed.paths | int | Number of path tiles placed |
| placed.stairs | int | Number of stairs placed (omitted if 0) |
| placed.platforms | int | Number of platforms placed (omitted if 0) |
| skipped | int | Tiles that failed to place |
| connectorEdgesInGrid | int | Total stair/ramp edges found in the graph |
| stoppedAt | string | "x,y" where routing stopped (sections limit or error) |
| errors | array | Error objects with `error` or `prefab`+`error` fields |
| timings | object | When `timings:true`: totalMs, snapshotMs, graphMs, astarMs, placementMs, placementsAttempted, graphNodes, pathNodes, pathEdges |

---

## Events (WebSocket)

Game events ship as server-push frames on the mod's WebSocket channel rather than HTTP webhooks. Connect to `ws://<host>:<wsPort>/api/ws` (default `ws://127.0.0.1:8086/api/ws`) and filter inbound frames for `type == "event"`:

```json
{
  "type": "event",
  "payload": {
    "event": "drought.start",
    "day": 45,
    "timestamp": 1711300000,
    "data": {"duration": 8}
  }
}
```

- Frame envelope, auth (`Authorization: Bearer <token>` or `?token=…`), reconnect semantics, and the full message-type catalog are in [websocket-protocol.md](websocket-protocol.md).
- The event catalog (≈70 events) and consumer examples (`tbot listen`, custom WS clients) are in [events.md](events.md).
- The pre-v0.9 outbound HTTP webhook endpoints (`POST /api/webhooks`, `GET /api/webhooks`, `POST /api/webhooks/delete`) and the `webhooksEnabled` / `webhookBatchMs` / `webhookCircuitBreaker` / `webhookMaxPendingEvents` / `webhookValidateUrls` settings have been removed. Old `settings.json` files emit a one-line deprecation warning at load.

---

## Debug

### POST /api/debug

Reflection-based inspector for game internals. Navigates object graphs, lists fields/properties/methods, and calls methods with arguments. Results can be chained with `$`.

**CLI:** `tbot debug target:help`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| target | string | yes | `"help"`, `"get"`, `"fields"`, or `"call"` |
| path | string | varies | Dot-separated path from TimberbotService (e.g. `"_scienceService.SciencePoints"`) |
| filter | string | no | (fields only) Filter members by name substring |
| method | string | varies | (call only) Method name to invoke |
| arg0..argN | string | no | (call only) Method arguments. Vector3Int as `"x,y,z"`. `"$"` = last result |

#### Targets

| Target | Description | Required args |
|--------|-------------|---------------|
| `help` | List available targets, roots, and examples | none |
| `get` | Navigate an object chain and dump the result | `path` |
| `fields` | List fields, properties, and methods on an object | `path` (optional), `filter` (optional) |
| `call` | Call a method on an object with typed arguments | `path`, `method`, `arg0`..`argN` |

#### Response (help)

```json
{
  "targets": ["help. this message", "get. navigate object chain", "fields. list members", "call. call method"],
  "roots": ["_buildingService", "_entityRegistry", "_districtCenterRegistry", "_navMeshService", "..."],
  "examples": [
    "debug target:fields path:_navMeshService filter:Road",
    "debug target:get path:_scienceService.SciencePoints",
    "debug target:call path:_navMeshService method:AreConnectedRoadInstant arg0:120,142,2 arg1:130,142,2"
  ]
}
```

#### Path syntax

- Dot-separated: `_fieldName.PropertyName.NestedField`
- List indexing: `_entityRegistry.Entities.[0]`
- GetComponent: `~TypeName` (e.g. `~MechanicalNode`)
- Chain from last result: `$.PropertyName`

!!! warning "Debug only"
    This endpoint uses reflection on game internals. It may break on Timberborn updates. Not intended for production automation.

---

### POST /api/benchmark

Profile internal hot paths and server micro-benchmarks. Requires `debugEndpointEnabled: true` in settings.json.

**CLI:** `tbot benchmark iterations:100`

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| iterations | int | no | Number of measured iterations per benchmark case (default 100) |

#### Response

Returns per-case timing and GC allocation counts for internal micro-benchmarks. Benchmark work is queued and advanced incrementally across frames so the request may take longer wall-clock time without monopolizing one frame.

!!! warning "Debug only"
    Requires `debugEndpointEnabled: true`. Returns `{"error": "disabled: benchmark endpoint"}` when disabled.

---

## Python CLI Helpers

These are convenience methods in the `tbot` CLI that have no direct HTTP equivalent.

### map

Colored ASCII grid with terrain height display. Background shading encodes z-level, foreground characters represent entities.

```bash
tbot map x1:112 y1:126 x2:132 y2:146
```

| Char | Color | Meaning |
|------|-------|---------|
| `0`-`9` | dim (green if moist) | Empty ground (digit = z % 10, background band = tens) |
| `~` | blue | Water |
| `@` | white | Entrance |
| `=` | yellow | Path |
| `D` | bright yellow | District center |
| `H` | yellow | Housing (Rowhouse, Barrack, Lodge) |
| `R` | yellow | Breeding pod |
| `F` | cyan | Farmhouse |
| `f` | green | Forester |
| `M` | white | Lumber mill / wood workshop |
| `S` | white | Science (Inventor, Numbercruncher) |
| `E` | bright yellow | Power (wheel, shaft) |
| `L` | red | Lumberjack |
| `G` | magenta | Gatherer |
| `K` | red | Hauling |
| `$` | yellow | Warehouse / pile |
| `P` | bright blue | Pump |
| `W` | bright blue | Tank |
| `X` | cyan | Floodgate / dam / levee / sluice |
| `C` | red | Campfire |
| `T` | green | Grown tree (Pine, Birch, Oak, Maple, Chestnut) |
| `t` | dim green | Seedling |
| `B` | magenta | Berry bush, shrub |
| `/` | yellow | Stairs |
| `_` | yellow | Platform |
| `m` | white | Metalsmith, smelter |
| `g` | white | Gear workshop |
| `b` | magenta | Bot assembler, bot part factory |
| `z` | magenta | Charging station |
| `V` | bright blue | Fluid dump |
| `v` | bright blue | Shower, swimming pool |
| `~` | green | Amenity (scratcher, bench, exercise plaza, medical bed) |
| `*` | red/yellow | Decoration (brazier, lantern, beaver bust) |
| `^` | dim | Roof |
| `R` | dim | Ruins, relics |
| `Q` | bright yellow | Wonders (monuments, flame, tribute, repopulator) |
| `A` | bright blue | Aquifer drill |
| `i` | dim | Automation (lever, sensor, timer, memory, relay) |
| `x` | red | Explosives (dynamite, detonator) |
| `#` | dim | Terrain block, dirt excavator |
| `!` | yellow | Banners, firework launcher |
| `\|` | dim | Fences (metal, wood) |
| `k` | bright green | Kohlrabi |
| `c` | bright green | Carrot |
| `p` | bright green | Potato |
| `w` | bright green | Wheat |
| `a` | bright green | Cassava |
| `s` | bright green | Sunflower |
| `n` | bright green | Corn |
| `e` | bright green | Eggplant |
| `y` | bright green | Soybean |
| `o` | bright green | Canola |
| `l` | bright green | Cattail |
| `d` | bright green | Spadderdock |

Background bands: z=0-9 (dark grays), z=10-19 (medium grays), z=20-22 (bright).

### find (CLI-only)

Find entities by name and/or proximity.

```bash
tbot find source:buildings name:Pump x:120 y:130 radius:20
```

### place_path (CLI-only)

A* path routing with auto-stairs/platforms. Wraps `POST /api/path/place`.

```bash
tbot place_path x1:120 y1:130 x2:150 y2:160
tbot place_path x1:0 y1:0 x2:255 y2:255 style:straight
tbot place_path x1:120 y1:130 x2:150 y2:160 sections:1 timings:true
```

### launch (CLI-only)

Prepare a save launch using `autoload.json`.

- On Windows, this also opens Timberborn via Steam.
- On macOS, v1 writes `autoload.json` and then expects you to open Timberborn manually.

```bash
tbot launch settlement:Potato save:Tomato
```

### top (CLI-only)

Live colony dashboard. Population, resources, weather, drought countdown, wellbeing breakdown, alerts.

```bash
tbot top
```

### Spatial memory (CLI-focused)

Persistent colony knowledge in `Documents/Timberborn/Mods/Timberbot/memory/`.

```bash
tbot brain            # live summary + persistent goal/tasks/locations
tbot brain goal:"get to 77 wellbeing"  # set persistent goal
tbot set_location name:berries x:120 y:140 note:"south of DC"  # save a named location
tbot remove_location name:berries     # remove a saved location
tbot list_locations   # list saved locations
tbot add_task action:"build roads"   # add task to work queue
tbot update_task id:1 status:done    # update task status
tbot list_tasks       # show all tasks
tbot clear_tasks      # remove done tasks
```

`brain` returns live summary (always fresh from `/api/summary`) plus persistent state from `memory/brain.toon` (goal, tasks, locations). Summary is never persisted. only goal, tasks, and locations survive between sessions. Set a persistent goal with `brain goal:"text"`. The built-in in-game agent also uses `brain` internally during startup before it launches Claude/Codex.

---

## Reference

### IDs and Names

- **Building IDs** are deterministic 32-bit hashes of internal game GUIDs. They are persistent and will survive game restarts and colony reloads.
- **Prefab names** come from `GET /api/prefabs`. Include faction suffix (e.g. `"LumberjackFlag.IronTeeth"`).
- **Good names** match Timberborn internal names: `Water`, `Log`, `Plank`, `Berries`, `Bread`, etc.
- **Building names** in responses are cleaned: `(Clone)`, `.IronTeeth`, `.Folktails` suffixes removed.

### Priority Values

| Value | Description |
|-------|-------------|
| `VeryLow` | Lowest priority |
| `Normal` | Default |
| `VeryHigh` | Highest priority |

Two priority types exist per building: `construction` (while building) and `workplace` (when finished). Set both on new buildings.

### Orientation

| Value | Name | Description |
|-------|------|-------------|
| 0 | south | Default facing |
| 1 | west | 90 degrees clockwise |
| 2 | north | 180 degrees |
| 3 | east | 270 degrees clockwise |

Coordinates always refer to the bottom-left corner of the footprint regardless of orientation. Python CLI accepts names (`south`, `west`, `north`, `east` or `s`/`w`/`n`/`e`).

### Crop Names

`Kohlrabi`, `Cassava`, `Carrot`, `Potato`, `Wheat`, `Sunflower`, `Corn`, `Eggplant`, `Soybean`

### Import Options

| Value | Description |
|-------|-------------|
| `None` | No importing |
| `Allowed` | Import when needed |
| `Forced` | Always import |

---

## Known Issues

!!! bug "Ghost Buildings"
    `POST /api/building/place` may create ghost buildings on invalid spots. The `Place()` callback fires and creates an entity even when placement is invalid. Python-side validation blocks most cases, but multi-tile overlaps or bad terrain can still ghost.

    **Never test placement carelessly**. every failed `Place()` may create a ghost that needs manual cleanup.
