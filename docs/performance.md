# Performance

Single source of truth for Timberbot API performance. All optimization decisions reference here.

## Open issues

| # | Severity | Issue | Cost | Location |
|---|---|---|---|---|
| 38 | High | Webhook `PendingPayloads` backlog is unbounded while endpoint is slow/failing | memory growth under noisy events until circuit breaker trips | `TimberbotEvents.cs` |
| 39 | High | HTTP `_pending` / `_writeQueue` POST queues are unbounded | unbounded memory + rising latency under write flood | `TimberbotHttpServer.cs` |
| 40 | Medium | `TimberbotLog.Info/Error` does synchronous `Debug.Log` + `File.AppendAllText` in hot paths | lock contention, disk I/O, string churn on requests/webhooks | `TimberbotLog.cs`, `TimberbotHttpServer.cs`, `TimberbotEvents.cs` |
| 41 | Medium | `Process` objects in agent paths are never disposed | OS handle leak across repeated start/stop sessions | `TimberbotAgent.cs` |
| 14 | Low | `CollectBuildings` basic: `$"{a}/{b}"` per building with workers | N strings | `TimberbotReadV2.cs` |
| 13 | Low | `CollectAlerts`: `$"{a}/{b}"` per unstaffed building | N strings | `TimberbotReadV2.cs` |
| 15 | Low | `CollectBeavers` basic: string concat `critical + "+" + n.Id` | N strings | `TimberbotReadV2.cs` |
| 16 | Low | `CollectPowerNetworks`: inner `List<PowerBuildingItem>` + `.ToArray()` per network | ~N networks | `TimberbotReadV2.cs` |
| 18 | Low | `CollectNotifications`: `.ToString()` per notification field | 2N strings | `TimberbotReadV2.cs` |
| 12 | Low | `CollectSummary` toon: `$"..."` interpolations for beds/workers/alerts + `string.Join` | ~7 strings | `TimberbotReadV2.cs` |
| 34 | Low | `_alertCounts` dictionary keys grow unbounded across alert types | capacity never shrinks | `TimberbotReadV2.cs` |
| 33 | Low | `InvalidPrefabError` iterates all buildings with `ToLowerInvariant()` + `new List` | error path only | `TimberbotPlacement.cs` |
| 35 | Low | `ShowPresetMenu` recreates buttons on every dropdown open | GC'd VisualElements | `TimberbotPanel.cs` |
| 36 | Low | `QueueTooltip` closure + scheduled callback per hover | lambda capture per hover | `TimberbotPanel.cs` |
| 37 | None | `RegisterTooltipHandlers` creates 3 lambda closures per settings row | 33 closures at build time | `TimberbotPanel.cs` |
| 21 | None | Unity GC spikes freeze all threads | 0.5-2s | unavoidable |
| 22 | None | `sb.ToString()` alloc per HTTP response | 100-500KB | unavoidable |

Highest-priority remaining work is queue/backlog bounding and hot-path logging reduction. Benchmark with `/api/benchmark` after fixing those to re-measure the lower-level alloc issues.

For thread model, snapshot pipeline, serialization, and reusable collections see [architecture.md](architecture.md).

## GC pressure

Every heap allocation in a Unity mod contributes to GC pressure. Unity uses a stop-the-world garbage collector. when enough garbage accumulates, ALL threads pause (0.5-50ms). Our mod runs inside someone else's game. Every allocation we make adds to the pile that triggers the game's GC stutter.

Goal: allocate once at game load, reuse forever. The only per-request allocation should be the final `ToString()` to produce the HTTP response.

### Allocation architecture

```
Game loads
  -> allocate TimberbotJw, Dictionaries, Lists, projection buffers
  -> done allocating

Every frame (main thread, on demand)
  -> ReadV2.ProcessPendingRefresh(): capture live state into DTO buffers (bounded budget)
  -> background finalize: publish immutable snapshots from captured data

Every HTTP GET (background thread)
  -> read from published snapshots (zero alloc)
  -> write into existing StringBuilder via TimberbotJw (zero alloc)
  -> ToString() to create response string (1 alloc, unavoidable)
```

### Snapshot capture (main thread, on demand)

Total measured cost: ~0.4ms/sec (0.04% of frame budget at 60fps).

**Zero-alloc (confirmed):**

| What | Count/sec | How |
|---|---|---|
| Building property reads | 500 | Direct field reads from cached component refs |
| Natural resource reads | 3000 | Direct field reads |
| Beaver reads + needs | 80 | Direct field reads |
| `CleanName()` for workplace/district | 80 | RefChanged skips unless ref changes (99% pointer compare) |
| `Inventory.Clear()` + repopulate | 500 | Dict allocated once, reused via Clear() |
| `NutrientStock.Clear()` + repopulate | 5 | Same pattern, only breeding pods |
| `Needs.Clear()` + repopulate | 80 | List allocated once, reused via Clear() |
| `new CachedNeed{...}` struct | 2400 | Struct = stack alloc, not heap |
| Inventory for-loop (indexed) | 500 | `for (int ii = 0; ...)`. no enumerator boxing |
| District refresh | 1-3 | Reuses existing CachedDistrict objects, clears Dict |
| Snapshot publish | on demand | Immutable DTO snapshot replaces previous via ref swap |

**Known allocations (accepted):**

| What | Count/sec | Severity | Status |
|---|---|---|---|
| `Math.Round(need.Points, 2)` | 2400 | None | 0 GC0 across 11.4M calls (benchmarked) |
| `foreach GetNeeds()` enumerator | 80 | None | 0 GC0 (10K benchmark) |
| `GetNeed(id)` / `GetNeedWellbeing(id)` | 2400 | None | Returns cached, zero-alloc |
| `foreach BreedingPod.Nutrients` | 5 | None | 0 GC0 (10K benchmark) |

### HTTP response (per request, background thread)

**Zero-alloc:** `_jw.Reset()`, `Key/Int/Str/Bool/Float`, `OpenObj/CloseObj`, auto-separator commas.

**Accepted allocations:**

| What | Count | Why accepted |
|---|---|---|
| `jw.ToString()` | 1 per request, 100-500KB | Unavoidable. HTTP needs the string |
| `StreamWriter` internal buffer | 1 per request, ~1KB | .NET runtime |
| `$"{interpolation}"` in toon endpoints | ~5 per summary | Negligible (low issues #12-14) |

### Webhooks (main thread, only with subscribers)

No subscribers (common case): `PushEvent()` returns immediately when `_webhooks.Count == 0`. Zero allocations.

With subscribers: `TimberbotJw` payload string (1 per event), field-level `_webhookSb` (reused), `sb.ToString()` per flush, `new StringContent()` on ThreadPool. 200ms batching window, circuit breaker at 30 failures.

### Entity lifecycle (per add/remove)

Entity registration in `TimberbotEntityRegistry` + projection buffer slot allocation. Once per entity lifetime. Webhook events only with subscribers.

## Benchmarks

### Serialization A/B test (trees, 2985 items)

Dictionary 4.7ms, Anonymous objects 13.8ms (worst. Newtonsoft reflection), StringBuilder **2.0ms** (winner).

### Micro-benchmarks (10K iterations, /api/benchmark)

| Test | GC0 | ms/call | Total calls | Verdict |
|---|---|---|---|---|
| `NeedMgr.GetNeeds.foreach` | **0** | 0.110 | 760,000 | Zero-alloc |
| `NeedMgr.FullNeedLoop` | **0** | 0.319 | 760,000 | All three calls zero-alloc |
| `BreedingPod.Nutrients` foreach | **0** | 0.006 | 60,000 | Zero-alloc |
| `Inventories.foreach` | **0** | 0.056 | 522,000 | Zero-alloc |
| `Inventories.forLoop` | **0** | 0.045 | 522,000 | 24% faster than foreach |
| `Inventories.AllInventories.only` | **0** | 0.020 | 522,000 | Just accessing inventories |
| `Inventories.FullRefreshSim` | **0** | 0.058 | 522,000 | Full production loop |
| `MathRound.vs.Manual` | **0/0** | 0.010/0.005 | 11,400,000 | 1.8x slower, no alloc |
| `StringInterpolation` | - | - | - | #12-14: pending benchmark run |
| `StringConcat.Needs` | - | - | - | #15: pending benchmark run |
| `GetBeaverNeeds` | - | - | - | #20: pending benchmark run |

### Endpoint performance (measured, 522 buildings / 2986 trees / 65 beavers)

| Endpoint | Items | Min (ms) | Notes |
|---|---|---|---|
| `ping` | 1 | **0.7** | Listener thread |
| `summary` | 3500+ | **0.9** | TimberbotJw, cached primitives |
| `buildings` | 522 | **2.1** | TimberbotJw |
| `buildings detail:full` | 522 | **3.4** | TimberbotJw, all fields |
| `trees` | 2983 | **8.6** | TimberbotJw |
| `gatherables` | 1504 | **6.7** | TimberbotJw |
| `beavers` | 65 | **1.1** | TimberbotJw |
| `beavers detail:full` | 65 | ~1.5 | all 38 needs |
| `alerts` | 19 | **0.8** | TimberbotJw |
| `resources` | 13 | **0.8** | district registries |
| `power` | cached | **0.8** | groups by PowerNetworkId |
| `weather` | 1 | **0.8** | service fields |
| `time` | 1 | **0.8** | service fields |
| `prefabs` | 157 | **2.9** | building templates |
| `wellbeing` | 1 | **0.9** | cached beaver needs |
| `tree_clusters` | 5 | **0.9** | cached natural resources |
| `map` (stacking) | varies | ~10 | cached tile footprints |
| **burst (7 calls)** | - | **17** | 2ms avg per call |

### Late-game projections

| Endpoint | Current items | Per-item | Late-game items | Projected |
|---|---|---|---|---|
| `buildings` | 522 | 4.0us | 1500 | ~6ms |
| `buildings full` | 522 | 6.5us | 1500 | ~10ms |
| `trees` | 2983 | 2.9us | 5000 | ~14ms |
| `gatherables` | 1504 | 4.5us | 3000 | ~13ms |
| `beavers` | 65 | 16.9us | 250 | ~4ms |
| `summary` | 3500+ | 0.26us | 10000 | ~3ms |
| `map` (region) | varies | region-bounded | larger builds | ~20ms |
| **Burst (7 calls)** | - | - | - | **~30ms** |

All scaling is linear with item count. Bot polling at 1/min cadence. even 30ms burst is imperceptible.

### Late-game risk factors

- **Bots scale free:** bots don't eat/drink/sleep but DO add to beaver index. 50+ bots = more entities to cache and serialize
- **Multi-district:** each district has its own resource counters. 3+ districts increases summary/resources iteration
- **Vertical builds:** heavy platform/stair stacking increases map `occupants` list sizes (lists reused, but more items per tile)
- **Power networks:** complex power grids fragment into many small networks. `power` endpoint iterates all buildings per call

### Allocation grades

| Layer | Frequency | Allocs/sec (steady state) | Grade |
|---|---|---|---|
| Snapshot capture | On demand | **0** (reusable buffers, confirmed by benchmark) | **A+** |
| HTTP GET response | On demand | 1 (ToString) + ~5 small strings | **A-** |
| Derived endpoints (alerts, power) | On demand | 1-2 (`.ToArray()` copies) | **A-** |
| Webhook (no subscribers) | N/A | 0 | **A+** |
| Webhook (with subscribers) | 5Hz flush | ~5-20 (TimberbotJw strings only) | **A-** |
| Entity lifecycle | Rare | N per entity | **A** (expected) |

## Optimization history

| Change | trees | buildings | buildings full | burst (7 calls) |
|---|---|---|---|---|
| Baseline (full entity scan) | ~50ms est | ~20ms est | ~30ms est | ~150ms est |
| Typed entity indexes | 29ms | 9ms | 10ms | 67ms |
| Event-driven (EventBus) | 28ms | 9ms | 13ms | 62ms |
| Cached component refs | 25ms | 8ms | 13ms | 64ms |
| GETs on listener thread | 23ms | 6.5ms | 8ms | 39ms |
| Cached primitives + snapshot buffers | 4.7ms | 2.8ms | 1.3ms | 28ms |
| StringBuilder (trees) | 2.0ms | 2.8ms | 1.3ms | 28ms |
| Alloc-once + SB buildings | 2.0ms | ~1ms | ~1ms | ~20ms est |
| DRY (Jw + ReadV2 snapshots) | **8.6ms** | **2.1ms** | **3.4ms** | **17ms** |

## Closed issues

| # | Issue | Resolution |
|---|---|---|
| 1 | Thread-unsafe: `CollectTreeClusters` reads Unity components on background thread | Uses cached `nr.Alive`, `nr.X/Y/Z`, `nr.Grown` |
| 2 | Thread-unsafe: `CollectFoodClusters`. same as #1 | Same approach |
| 3 | `CollectSummary` json: `JsonConvert.DeserializeObject` re-parses cluster JSON | `WriteClustersFiltered` builds inline via JW, zero Newtonsoft |
| 4 | `CollectSummary`: ~20 temp collections per call | Hoisted to field-level dicts/sets, cleared per call. Static roleMap/cropNames |
| 5 | `CollectBuildings` full toon: new StringBuilder x2 per building | Reuses field-level `_invSb`/`_recSb`, cleared per building |
| 6 | `CollectTreeClusters`: new Dictionary x2 + new int[] per cell | Inner `int[]` and `Dictionary<string, int>` reused via clear-in-place, zero alloc at steady state |
| 7 | `CollectFoodClusters`: same as #6 | Same approach |
| 8 | `CollectTiles`: new Dictionary + 3 HashSet + StringBuilder per tile | Inner `List<(string, int)>` reused via clear-in-place, zero alloc at steady state |
| 24 | `CollectTiles` inner list allocs per tile | Inner lists cleared in place, not re-created. Zero alloc at steady state |
| 25 | `CollectTreeClusters`/`CollectFoodClusters` inner `int[]` + `Dictionary` per cell | Inner arrays reset in place, inner dicts cleared. Zero alloc at steady state |
| 26 | `BuildAlertsFromBuildings` `new List` per call | Field-level `_alertBuffer`, cleared per call. `.ToArray()` remains (1 alloc) |
| 27 | `BuildPowerFromBuildings` `new Dictionary` per call | Field-level `_powerNetworks`, cleared per call. Inner `List`/`.ToArray()` remain |
| 28 | `CollectWellbeing` 4 new Dicts + N Lists per call | All 4 dicts hoisted to field-level. Inner `List<NeedSpec>` reused via clear-in-place |
| 17 | `CollectWellbeing`: 4 Dicts + List per group | Superseded by #28. all dicts field-level |
| 9 | `CollectDistribution` GetComponent on background thread | Pre-built on main thread via `RefreshMainThreadData()` |
| 10 | `CollectScience` GetSpec on background thread | Pre-built on main thread via `RefreshMainThreadData()` |
| 11 | District refresh allocates new CachedDistrict + Dict every 1s | Reuses existing CachedDistrict objects, updates in place, clears Dict |
| 23 | `Math.Round(need.Points, 2)` boxes on Mono | **DISPROVED**. 0 GC0 across 11.4M calls |
| 29 | `SaveSettingToken` File.Read + JObject.Parse + File.Write per keystroke | `_cachedSettings` JObject in memory, 1s debounce flush via `FlushSettingsIfNeeded()` in `UpdateSingleton()`, flush on `Unload()` |
| 30 | Selected-object reflection alloc every 0.5s in panel | Removed with the selected-object inspector UI; panel no longer polls selection or reflects `gameObject` accessors |
| 31 | `new StringBuilder(64)` per building per snapshot for StatusAlerts | `BuildingCompactState.StatusAlerts` changed from `string` to `string[]`, stores game strings directly |
| 32 | `string.Split(',')` per building in summary alert counting | Consumers iterate `string[]` directly, no Split/Trim/array allocation |
| 37 | `RegisterTooltipHandlers` 3 closures per settings row (33 total) | Created once in `BuildModal()` at load time, not per-frame. Correct pattern for UI event handlers |
| 20 | `GetBeaverNeeds()` LINQ iterator alloc on background thread | Decompiled: read-only filter over write-once list, not a thread-safety bug. Cached as `_cachedBeaverNeeds` array at load time, zero alloc on read path |
| - | `Priority.ToString()` per building per refresh | Static lookup array |
| - | `CleanName()` per employed beaver per refresh | RefChanged pattern (ref compare) |
| - | `GetComponent<EntityComponent>()` per beaver per refresh | Cached `Go` field at add-time |
| - | Building X/Y/Z/Orientation re-read per refresh | Moved to add-time (immutable) |
| - | `foreach Inventories.AllInventories` / `inv.Stock` | Indexed for-loop (no enumerator boxing) |
| - | 60fps refresh rate | Cadenced to 1Hz (configurable) |
| - | `Formatting.Indented` whitespace bloat | Switched to `Formatting.None` (~30% smaller) |
| - | `GetBytes()` byte array alloc per response | `StreamWriter` writes directly to output stream |
| - | UTF-8 BOM prefix from StreamWriter | `new UTF8Encoding(false)` |
| - | `new StringBuilder(256)` per webhook per flush | Reuses field-level `_webhookSb` |
| - | `JsonConvert.SerializeObject` per webhook event | TimberbotJw writes directly |
| - | `Key().OpenArr()` double-comma bug | `_hasValue[_depth] = false` after Key() |
| - | Value methods missing `AutoSep()` | All value methods now call AutoSep() |
| - | `Float(v).ToString(fmt)` string alloc | Zero-alloc digit writing to SB |
| - | Partial JSON on prefab cost exception | Collect-then-emit pattern |
| - | Webhook rate limiting | 200ms batching window (configurable) |
| - | Webhook circuit breaker | 30 consecutive failures disables webhook |
| - | TimberbotService monolith | Split into 8 classes with focused DI |
| - | RefreshCachedState error isolation | Per-entity try/catch in all 3 loops |

For test coverage and how to run tests, see [developing.md](developing.md#testing).
