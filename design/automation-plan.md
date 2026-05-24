# Extending Timberbot: Automation Wiring & Configuration

> **Status:** design + decompiled API surface — partial implementation. See [`../docs/api-reference.md`](../docs/api-reference.md) `/api/automation/*` for what is shipped today.

Add full support for Timberborn 1.0's automation system — wiring sensors to relays to buildings, and configuring thresholds/modes on every automation component.

> **Note:** The mod already has no external modding framework dependencies (no BepInEx, no Harmony). It references only vanilla game DLLs via `Publicize="true"`. We continue this pattern for the automation DLLs.

## Decompiled API Surface (from ILSpy)

To inspect the full surface, decompile `Timberborn.Automation.dll` and `Timberborn.AutomationBuildings.dll` locally with `ilspycmd` (see `docs/devenv.md`).

### Core Wiring API (`Timberborn.Automation`)

| Class | Key Methods/Properties |
|---|---|
| `Automator` | `SetState(bool)`, `AddInput()`, `InputConnections`, `OutputConnections`, `State`, `IsTransmitter`, `AutomatorName`, `AutomatorId` |
| `AutomatorConnection` | `Connect(Automator)`, `Disconnect()`, `State`, `BooleanState`, `IsConnected`, `Transmitter`, `Receiver` |
| `Automatable` | `SetInput(Automator)`, `Input`, `IsAutomated`, `State` |
| `AutomatorRegistry` | `Automators`, `Transmitters`, `FindTransmitterById(Guid)` |
| `AutomationRunner` | `Register()`, `Unregister()`, `MergePartitions()`, `Schedule()` |

### Sensor/Relay Components (`Timberborn.AutomationBuildings`)

| Component | Configurable Properties | Setter Methods |
|---|---|---|
| `DepthSensor` | `Threshold`, `Mode` (NumericComparisonMode) | `SetThreshold(float)`, `SetMode(NumericComparisonMode)` |
| `ContaminationSensor` | `Threshold`, `Mode` | `SetThreshold(float)`, `SetMode(NumericComparisonMode)` |
| `FlowSensor` | `Threshold`, `Mode` | `SetThreshold(float)`, `SetMode(NumericComparisonMode)` |
| `ResourceCounter` | `GoodId`, `Threshold`, `FillRateThreshold`, `Mode` (StockLevel/FillRate), `ComparisonMode`, `IncludeInputs` | `SetGoodId(string)`, `SetThreshold(int)`, `SetFillRateThreshold(float)`, `SetMode(ResourceCounterMode)`, `SetComparisonMode(NumericComparisonMode)`, `SetIncludeInputs(bool)` |
| `PopulationCounter` | `Threshold`, `Mode`, `ComparisonMode`, `GlobalMode`, `CountBeavers`, `CountBots` | `SetThreshold(int)`, `SetMode(PopulationCounterMode)`, `SetComparisonMode(NumericComparisonMode)`, `SetGlobalMode(bool)`, `SetCountBeavers(bool)`, `SetCountBots(bool)` |
| `ScienceCounter` | (similar pattern) | (similar) |
| `PowerMeter` | `IntThreshold`, `PercentThreshold`, `Mode`, `ComparisonMode` | `SetMode(PowerMeterMode)`, `SetComparisonMode(NumericComparisonMode)`, `SetIntThreshold(int)`, `SetPercentThreshold(float)` |
| `Relay` | `Mode` (Not/And/Or/Xor/Passthrough), `InputA`, `InputB` | `SetMode(RelayMode)`, `SetInputA(Automator)`, `SetInputB(Automator)` |
| `Memory` | `Mode` (MemoryMode), `InputA`, `InputB`, `ResetInput` | `SetMode(MemoryMode)`, `SetInputA(Automator)`, `SetInputB(Automator)`, `SetResetInput(Automator)` |
| `Lever` | `SpringReturn`, `Pinned` | `SetSpringReturn(bool)`, `SetPinned(bool)` |
| `Timer` | (intervals) | (via TimerInterval) |
| `Chronometer` | `StartTime`, `EndTime`, `Mode` | `SetStartTime(float)`, `SetEndTime(float)`, `SetMode(ChronometerMode)` |
| `WeatherStation` | (weather conditions) | (similar) |
| `Gate` | `OpenMode`, `ClosedMode`, `AutomatedMode` | (via GateOpeningMode) |

### Key Enums

```csharp
enum NumericComparisonMode { Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual }
enum RelayMode { Not, And, Or, Xor, Passthrough }
enum ResourceCounterMode { FillRate, StockLevel }
enum AutomatorState { Off, On, Error }
enum ConnectionState { Disconnected, Off, On }
```

### How Wiring Works (from decompiled code)

To wire a sensor output to a building's pause input:
1. Get the **source** entity's `Automator` component (sensor/relay — it's an `ITransmitter`).
2. Get the **target** entity's `Automatable` component (any pausable building) or `Relay`/`Memory` component.
3. Call `target.Automatable.SetInput(sourceAutomator)` — this internally calls `AutomatorConnection.Connect()` which merges partitions and schedules re-evaluation.

For **Relay** inputs specifically: call `relay.SetInputA(sourceAutomator)` and `relay.SetInputB(otherAutomator)`.

## Proposed Changes

---

### DLL References
#### [MODIFY] `Timberbot.csproj`
Add two new publicized references:
```xml
<Reference Include="Timberborn.Automation" Publicize="true">
  <Private>false</Private>
  <HintPath>$(GameManagedDir)\Timberborn.Automation.dll</HintPath>
</Reference>
<Reference Include="Timberborn.AutomationBuildings" Publicize="true">
  <Private>false</Private>
  <HintPath>$(GameManagedDir)\Timberborn.AutomationBuildings.dll</HintPath>
</Reference>
```
Also add any transitive dependencies (`Timberborn.Goods`, `Timberborn.CoreSound`, etc.) if not already present.

---

### Read Endpoints — Automation State on Buildings
#### [MODIFY] `TimberbotReadV2.cs`
When serializing a building, check for `Automator`, `Automatable`, and specific sensor components. Append:
```json
{
  "id": 42,
  "name": "ContaminationSensor",
  "automation": {
    "type": "ContaminationSensor",
    "state": "On",
    "config": {
      "threshold": 0.5,
      "comparisonMode": "Greater"
    },
    "outputs": [{"id": 44, "name": "Relay #1"}]
  }
}
```
For a regular building with `Automatable`:
```json
{
  "id": 44,
  "name": "WaterPump",
  "automation": {
    "isAutomated": true,
    "inputState": "On",
    "input": {"id": 42, "name": "ContaminationSensor"}
  }
}
```

---

### Write Endpoints — Wiring & Configuration
#### [MODIFY] `TimberbotWrite.cs`

**New endpoint: `LinkAutomation`**
- JSON body: `{"sourceId": <int>, "targetId": <int>, "input": "a|b|reset"}`
- `sourceId`/`targetId` naming is necessary here because this endpoint takes two entities (unlike every other API endpoint which uses plain `id`)
- Resolves `sourceId` → `Automator` (must be `IsTransmitter`)
- Resolves `targetId` → checks for `Automatable`, `Relay`, or `Memory`
  - `Automatable`: calls `SetInput(sourceAutomator)`
  - `Relay`: calls `SetInputA(sourceAutomator)` or `SetInputB(sourceAutomator)` based on `input` param ("a" or "b")
  - `Memory`: calls `SetInputA/B/Reset` based on `input` param
- Returns the new connection state

**New endpoint: `UnlinkAutomation`**
- JSON body: `{"id": <int>, "input": "a|b|reset"}`
- Uses `id` (matches existing API convention for single-entity operations)
- Resolves `id` → `Automatable`/`Relay`/`Memory`
- Disconnects the specified input

**New endpoint: `ConfigureAutomation`**
- JSON body: `{"id": <int>, "property": "<string>", "value": "<string>"}`
- Uses `id` (matches existing API convention for single-entity operations)
- A polymorphic setter that inspects which automation component the building has and calls the appropriate setter:
- `DepthSensor` → `SetThreshold(float)`, `SetMode(NumericComparisonMode)`
- `ContaminationSensor` → same
- `FlowSensor` → same
- `ResourceCounter` → `SetGoodId(string)`, `SetThreshold(int)`, `SetFillRateThreshold(float)`, `SetMode(ResourceCounterMode)`, `SetComparisonMode(NumericComparisonMode)`, `SetIncludeInputs(bool)`
- `PopulationCounter` → `SetThreshold(int)`, `SetMode(PopulationCounterMode)`, `SetComparisonMode(...)`, `SetGlobalMode(bool)`, `SetCountBeavers(bool)`, `SetCountBots(bool)`
- `PowerMeter` → `SetMode(PowerMeterMode)`, `SetComparisonMode(...)`, `SetIntThreshold(int)`, `SetPercentThreshold(float)`
- `Relay` → `SetMode(RelayMode)`
- `Memory` → `SetMode(MemoryMode)`
- `Chronometer` → `SetStartTime(float)`, `SetEndTime(float)`, `SetMode(ChronometerMode)`
- `Lever` → `SetSpringReturn(bool)`, `SetPinned(bool)`

> **Naming convention:** The existing API uses plain `id` in every JSON body for single-entity operations (e.g. `{"id": 42, "paused": true}`). The C# method parameter names (like `buildingId`) are internal and never leak to the HTTP contract. Only `LinkAutomation` uses different names (`sourceId`/`targetId`) because it inherently operates on two entities.

---

### HTTP Routing
#### [MODIFY] `TimberbotHttpServer.cs`
Add route handlers for:
- `POST /api/automation/link` → `LinkAutomation`
- `POST /api/automation/unlink` → `UnlinkAutomation`
- `POST /api/automation/configure` → `ConfigureAutomation`

---

### DI Registration
#### [MODIFY] `TimberbotConfigurator.cs`
Inject `AutomatorRegistry` into `TimberbotWrite` (or `TimberbotReadV2`) so we can look up transmitters by entity ID.

---

### Python Client
#### [MODIFY] `timberbot.py`
Add CLI commands:
- `timberbot.py link source_id:<id> target_id:<id> [input:a|b|reset]`
- `timberbot.py unlink id:<id> [input:a|b|reset]`
- `timberbot.py configure_automation id:<id> property:<prop> value:<val>`

The Python CLI maps `source_id`/`target_id` params to `sourceId`/`targetId` JSON keys for the link endpoint, and `id` for the other two — matching the existing convention used by `pause`, `recipe`, `floodgate`, etc.

---

### Documentation
#### [MODIFY] `features.md`
- Remove the "By design" rows for Automation & Logic gates.
- Add new feature rows for automation wiring, sensor configuration, and relay logic.

#### [MODIFY] `api-reference.md`
- Document the three new endpoints with request/response examples.

---

## Open Questions

1. **Automation state on `GET /api/buildings`**: Should we always include the `automation` block, or only when `detail:full` is requested? Including it always adds JSON size but is more convenient for the AI.
2. **Timer configuration**: The `Timer` component uses `TimerInterval` objects with a serializer. Should we expose timer intervals in v1 or defer to a later release?
3. **Gate (physical gate building)**: The `Gate` has OpenMode/ClosedMode/AutomatedMode plus `GateOpeningMode` enum. Should we expose this, or is it too niche for the first iteration?

## Verification Plan

### Automated Tests
- `timberbot.py benchmark` to check performance impact of automation state serialization.

### Manual Verification
1. Place a `ResourceCounter` and a `WaterPump` in-game.
2. `timberbot.py configure_automation id:<counter_id> property:goodId value:Water`
3. `timberbot.py configure_automation id:<counter_id> property:threshold value:50`
4. `timberbot.py configure_automation id:<counter_id> property:comparisonMode value:Less`
5. `timberbot.py link source_id:<counter_id> target_id:<pump_id>`
6. Verify in the game UI that the wire appears and the pump pauses when water stock < 50.
