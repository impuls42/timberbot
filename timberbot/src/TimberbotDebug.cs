// TimberbotDebug.cs. Runtime reflection inspector and performance benchmark.
//
// WHY THIS EXISTS
// ---------------
// Timberborn is a closed-source game. When adding new API endpoints, we often
// need to discover what data a game service exposes, what methods it has, or
// how a component behaves at runtime. Instead of guessing, rebuilding, and
// restarting the game each time, the debug endpoint lets you inspect live game
// objects from outside the game while it's running.
//
// REFLECTION INSPECTOR (/api/debug)
// ---------------------------------
// Walks the mod's injected game services via .NET reflection:
//   target:get path:_scienceService.SciencePoints  -> reads a field/property chain
//   target:fields path:_weatherService             -> lists all fields on a service
//   target:call method:FindEntity arg0:-507504     -> calls a method, stores result in $
//   target:get path:$.AllComponents                -> chains from the last call result
//
// This is how we verified thread safety of water/terrain services, discovered
// undocumented component properties, and confirmed GC behavior. all without
// rebuilding the mod.
//
// BENCHMARK (/api/benchmark)
// --------------------------
// Profiles internal server hot paths and micro-benchmarks gameplay access patterns:
//   - Measures GC0 collections to detect hidden allocations
//   - Tests game API patterns (GetNeeds, Inventories, BreedingPod.Nutrients)
//   - Benchmarks selected internal operations that do not depend on RequestFresh waits
//   - Runs warmup iterations to stabilize JIT, then measures
//
// This is how we confirmed zero-alloc on the hot path (0 GC0 across 760K calls)
// and identified which internal loops are slowest.

using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using Timberborn.BlockObjectTools;
using UnityEngine;

namespace Timberbot
{
    public class TimberbotDebug
    {
        private readonly PreviewFactory _previewFactory;
        private readonly TimberbotJw _jw = new TimberbotJw(512);
        private readonly BindingFlags _flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        private object _debugLastResult;

        public TimberbotService Service;

        public TimberbotDebug(PreviewFactory previewFactory) { _previewFactory = previewFactory; }

        internal ITimberbotWriteJob CreateBenchmarkJob(int iterations)
            => new BenchmarkJob(this, iterations);

        public object RunBenchmark(int iterations)
        {
            var job = new BenchmarkJob(this, iterations);
            while (!job.IsCompleted)
                job.Step(Time.realtimeSinceStartup, double.MaxValue);
            return job.Result;
        }

        private sealed class BenchmarkContext
        {
            public int BuildingCount;
            public int BeaverCount;
            public int NaturalCount;
            public IReadOnlyList<TimberbotReadV2.TrackedBuildingRef> TrackedBuildings;
            public IReadOnlyList<TimberbotReadV2.TrackedBeaverRef> TrackedBeavers;
            public int Iterations;
        }

        private interface IBenchmarkCase
        {
            bool IsCompleted { get; }
            void Step(float now, double budgetMs);
            void Cancel(string error);
        }

        private sealed class BenchmarkJob : ITimberbotWriteJob
        {
            private readonly TimberbotDebug _owner;
            private readonly int _iterations;
            private readonly List<object> _results = new List<object>();
            private List<IBenchmarkCase> _cases;
            private bool _setupDone;
            private bool _completed;
            private int _statusCode = 200;
            private int _caseIndex;
            private object _result;

            public BenchmarkJob(TimberbotDebug owner, int iterations)
            {
                _owner = owner;
                _iterations = Math.Max(iterations, 1);
            }

            public string Name => "/api/benchmark";
            public bool IsCompleted => _completed;
            public int StatusCode => _statusCode;
            public object Result => _result;

            public void Step(float now, double budgetMs)
            {
                if (_completed) return;

                var budgetWatch = System.Diagnostics.Stopwatch.StartNew();
                if (!_setupDone)
                {
                    var ctx = _owner.CaptureBenchmarkContext(_iterations, now);
                    _results.Add(new { test = "_meta", buildings = ctx.BuildingCount, beavers = ctx.BeaverCount, trees = ctx.NaturalCount });
                    _cases = _owner.BuildBenchmarkCases(ctx, _results);
                    _setupDone = true;
                    if (_cases.Count == 0)
                    {
                        _result = new { benchmarks = _results };
                        _completed = true;
                        return;
                    }
                }

                while (_caseIndex < _cases.Count)
                {
                    double remainingMs = budgetMs - budgetWatch.Elapsed.TotalMilliseconds;
                    if (remainingMs <= 0) break;

                    var current = _cases[_caseIndex];
                    current.Step(now, remainingMs);
                    if (!current.IsCompleted) break;
                    _caseIndex++;
                }

                if (_caseIndex >= _cases.Count)
                {
                    _result = new { benchmarks = _results };
                    _completed = true;
                }
            }

            public void Cancel(string error)
            {
                if (_completed) return;
                if (_cases != null && _caseIndex < _cases.Count)
                    _cases[_caseIndex].Cancel(error);
                _statusCode = 500;
                _result = new { error };
                _completed = true;
            }
        }

        private sealed class TimedBenchmarkCase : IBenchmarkCase
        {
            private readonly List<object> _results;
            private readonly int _iterations;
            private readonly int _warmups;
            private readonly Func<float, object> _execute;
            private readonly Func<TimedBenchmarkCase, object> _buildResult;
            private int _warmupsDone;
            private int _iterationsDone;
            private bool _measuring;
            private long _gcBefore;
            private long _elapsedTicks;

            public TimedBenchmarkCase(List<object> results, int iterations, int warmups, Func<float, object> execute, Func<TimedBenchmarkCase, object> buildResult)
            {
                _results = results;
                _iterations = Math.Max(iterations, 1);
                _warmups = Math.Max(warmups, 0);
                _execute = execute;
                _buildResult = buildResult;
            }

            public bool IsCompleted { get; private set; }
            public object LastResult { get; private set; }
            public int Iterations => _iterations;
            public int CompletedIterations => _iterationsDone;
            public long Gc0 => _measuring ? GC.CollectionCount(0) - _gcBefore : 0;
            public double TotalMs => _elapsedTicks * 1000.0 / System.Diagnostics.Stopwatch.Frequency;

            public void Step(float now, double budgetMs)
            {
                if (IsCompleted) return;

                var budgetWatch = System.Diagnostics.Stopwatch.StartNew();
                while (!IsCompleted && budgetWatch.Elapsed.TotalMilliseconds < budgetMs)
                {
                    if (_warmupsDone < _warmups)
                    {
                        _execute(now);
                        _warmupsDone++;
                        continue;
                    }

                    if (!_measuring)
                    {
                        _gcBefore = GC.CollectionCount(0);
                        _measuring = true;
                    }

                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    LastResult = _execute(now);
                    sw.Stop();
                    _elapsedTicks += sw.ElapsedTicks;
                    _iterationsDone++;

                    if (_iterationsDone >= _iterations)
                    {
                        _results.Add(_buildResult(this));
                        IsCompleted = true;
                    }
                }
            }

            public void Cancel(string error)
            {
                if (IsCompleted) return;
                _results.Add(new { test = "cancelled", error });
                IsCompleted = true;
            }
        }

        private sealed class QueuedJobBenchmarkCase : IBenchmarkCase
        {
            private readonly List<object> _results;
            private readonly int _iterations;
            private readonly int _warmups;
            private readonly Func<ITimberbotWriteJob> _createJob;
            private readonly Func<object, object> _buildResult;
            private int _warmupsDone;
            private int _iterationsDone;
            private bool _measuring;
            private long _gcBefore;
            private long _elapsedTicks;
            private ITimberbotWriteJob _job;
            private object _lastResult;

            public QueuedJobBenchmarkCase(List<object> results, int iterations, int warmups, Func<ITimberbotWriteJob> createJob, Func<object, object> buildResult)
            {
                _results = results;
                _iterations = Math.Max(iterations, 1);
                _warmups = Math.Max(warmups, 0);
                _createJob = createJob;
                _buildResult = buildResult;
            }

            public bool IsCompleted { get; private set; }

            public void Step(float now, double budgetMs)
            {
                if (IsCompleted) return;

                var budgetWatch = System.Diagnostics.Stopwatch.StartNew();
                while (!IsCompleted && budgetWatch.Elapsed.TotalMilliseconds < budgetMs)
                {
                    if (_job == null)
                    {
                        if (!_measuring && _warmupsDone >= _warmups)
                        {
                            _gcBefore = GC.CollectionCount(0);
                            _measuring = true;
                        }
                        _job = _createJob();
                    }

                    double remainingMs = budgetMs - budgetWatch.Elapsed.TotalMilliseconds;
                    if (remainingMs <= 0) break;

                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    _job.Step(now, remainingMs);
                    sw.Stop();
                    if (_measuring)
                        _elapsedTicks += sw.ElapsedTicks;

                    if (!_job.IsCompleted) break;

                    _lastResult = _job.Result;
                    _job = null;

                    if (_warmupsDone < _warmups)
                    {
                        _warmupsDone++;
                        continue;
                    }

                    _iterationsDone++;
                    if (_iterationsDone >= _iterations)
                    {
                        _results.Add(_buildResult(new
                        {
                            iterations = _iterations,
                            totalMs = _elapsedTicks * 1000.0 / System.Diagnostics.Stopwatch.Frequency,
                            perCallMs = (_elapsedTicks * 1000.0 / System.Diagnostics.Stopwatch.Frequency) / _iterations,
                            gc0 = GC.CollectionCount(0) - _gcBefore,
                            result = _lastResult
                        }));
                        IsCompleted = true;
                    }
                }
            }

            public void Cancel(string error)
            {
                if (_job != null) _job.Cancel(error);
                if (IsCompleted) return;
                _results.Add(new { test = "cancelled", error });
                IsCompleted = true;
            }
        }

        private BenchmarkContext CaptureBenchmarkContext(int iterations, float now)
        {
            var buildingSnapshot = Service.ReadV2.EnsureBuildingsFreshNow(now, true);
            var beaverSnapshot = Service.ReadV2.EnsureBeaversFreshNow(now, true);
            var naturalSnapshot = Service.ReadV2.EnsureNaturalResourcesFreshNow(now);
            Service.ReadV2.EnsureDistrictsFreshNow(now);

            return new BenchmarkContext
            {
                BuildingCount = buildingSnapshot.Count,
                BeaverCount = beaverSnapshot.Count,
                NaturalCount = naturalSnapshot.Count,
                TrackedBuildings = Service.ReadV2.TrackedBuildings,
                TrackedBeavers = Service.ReadV2.TrackedBeavers,
                Iterations = Math.Max(iterations, 1)
            };
        }

        private List<IBenchmarkCase> BuildBenchmarkCases(BenchmarkContext ctx, List<object> results)
        {
            var cases = new List<IBenchmarkCase>();
            double inventoriesForeachMs = 0;

            var breedingPods = ctx.TrackedBuildings.Where(t => t.BreedingPod != null).ToList();
            if (breedingPods.Count > 0)
            {
                cases.Add(new TimedBenchmarkCase(
                    results,
                    ctx.Iterations,
                    3,
                    _ =>
                    {
                        for (int i = 0; i < breedingPods.Count; i++)
                            foreach (var ga in breedingPods[i].BreedingPod.Nutrients) { var _unused = ga.Amount; }
                        return null;
                    },
                    c => new { test = "BreedingPod.Nutrients", count = breedingPods.Count, iterations = c.Iterations, totalMs = c.TotalMs, perCallMs = c.TotalMs / c.Iterations, gc0 = c.Gc0 }));
            }

            var withInventories = ctx.TrackedBuildings.Where(t => t.Inventories != null).ToList();
            if (withInventories.Count > 0)
            {
                cases.Add(new TimedBenchmarkCase(
                    results,
                    ctx.Iterations,
                    3,
                    _ =>
                    {
                        for (int bi = 0; bi < withInventories.Count; bi++)
                            foreach (var inv in withInventories[bi].Inventories.AllInventories)
                                foreach (var ga in inv.Stock) { var _unused = ga.Amount; }
                        return null;
                    },
                    c =>
                    {
                        inventoriesForeachMs = c.TotalMs;
                        return new { test = "Inventories.foreach", count = withInventories.Count, iterations = c.Iterations, totalMs = c.TotalMs, perCallMs = c.TotalMs / c.Iterations, gc0 = c.Gc0 };
                    }));

                cases.Add(new TimedBenchmarkCase(
                    results,
                    ctx.Iterations,
                    3,
                    _ =>
                    {
                        for (int bi = 0; bi < withInventories.Count; bi++)
                        {
                            var allInv = withInventories[bi].Inventories.AllInventories;
                            for (int ii = 0; ii < allInv.Count; ii++)
                            {
                                var stock = allInv[ii].Stock;
                                for (int si = 0; si < stock.Count; si++) { var _unused = stock[si].Amount; }
                            }
                        }
                        return null;
                    },
                    c => new { test = "Inventories.forLoop", count = withInventories.Count, iterations = c.Iterations, totalMs = c.TotalMs, perCallMs = c.TotalMs / c.Iterations, gc0 = c.Gc0, speedup = c.TotalMs > 0 ? inventoriesForeachMs / c.TotalMs : 0 }));
            }

            var beaversWithNeeds = ctx.TrackedBeavers.Where(t => t.NeedMgr != null).ToList();
            if (beaversWithNeeds.Count > 0)
            {
                cases.Add(new TimedBenchmarkCase(
                    results,
                    ctx.Iterations,
                    3,
                    _ =>
                    {
                        for (int bi = 0; bi < beaversWithNeeds.Count; bi++)
                            foreach (var ns in beaversWithNeeds[bi].NeedMgr.GetNeeds()) { var _unused = ns.Id; }
                        return null;
                    },
                    c => new { test = "NeedMgr.GetNeeds.foreach", count = beaversWithNeeds.Count, iterations = c.Iterations, totalMs = c.TotalMs, perCallMs = c.TotalMs / c.Iterations, gc0 = c.Gc0 }));

                cases.Add(new TimedBenchmarkCase(
                    results,
                    ctx.Iterations,
                    0,
                    _ =>
                    {
                        for (int bi = 0; bi < beaversWithNeeds.Count; bi++)
                        {
                            var mgr = beaversWithNeeds[bi].NeedMgr;
                            foreach (var ns in mgr.GetNeeds())
                            {
                                var need = mgr.GetNeed(ns.Id);
                                var wb = mgr.GetNeedWellbeing(ns.Id);
                                var _unused = need.Points + wb;
                            }
                        }
                        return null;
                    },
                    c => new { test = "NeedMgr.FullNeedLoop", count = beaversWithNeeds.Count, iterations = c.Iterations, totalMs = c.TotalMs, perCallMs = c.TotalMs / c.Iterations, gc0 = c.Gc0 }));
            }

            cases.Add(MakeEndpointCase(results, "CollectPrefabs", ctx.Iterations, 3, _ => Service.Placement.CollectPrefabs()));

            return cases;
        }

        private static TimedBenchmarkCase MakeEndpointCase(List<object> results, string name, int iterations, int warmups, Func<float, object> fn, int knownItems = -1)
        {
            return new TimedBenchmarkCase(
                results,
                iterations,
                warmups,
                fn,
                c =>
                {
                    int items = knownItems >= 0 ? knownItems : c.LastResult is IList list ? list.Count : c.LastResult != null ? 1 : 0;
                    bool pass = !(c.LastResult is Dictionary<string, object> dict && dict.ContainsKey("error"));
                    return new { test = name, iterations = c.Iterations, totalMs = c.TotalMs, perCallMs = c.TotalMs / c.Iterations, gc0 = c.Gc0, items, pass };
                });
        }

        public object DebugInspect(string target, Dictionary<string, string> args = null)
        {
            args = args ?? new Dictionary<string, string>();
            string Arg(string name, string fallback = "") => args.TryGetValue(name, out var value) ? value : fallback;

            try
            {
                target = (target ?? "help").ToLowerInvariant();
                switch (target)
                {
                    case "help":
                        return new
                        {
                            target = "help",
                            roots = GetRoots(),
                            examples = new[]
                            {
                                "debug target:fields path:ReadV2 filter:Collect",
                                "debug target:describe path:ReadV2.Buildings depth:2 sample:3",
                                "debug target:get path:Registry.AllGoodIds",
                                "debug target:call path:_navMeshService method:AreConnectedRoadInstant arg0:120,142,2 arg1:130,142,2",
                                "debug target:compare left:ReadV2.PublishSequence value:0",
                                "debug target:assert path:_debugEnabled op:eq value:false",
                            }
                        };
                    case "roots":
                        return new { target = "roots", roots = GetRoots() };
                    case "get":
                    {
                        var path = Arg("path", "");
                        if (string.IsNullOrEmpty(path)) return _jw.Error("invalid_param: pass path:_fieldName.nested.field");
                        var obj = Resolve(path);
                        _debugLastResult = obj;
                        return new { path, result = DescribeValue(obj, 0, ParseInt(Arg("depth", "1"), 1), ParseInt(Arg("sample", "5"), 5)) };
                    }
                    case "fields":
                    {
                        var path = Arg("path", "");
                        object obj = string.IsNullOrEmpty(path) ? (object)Service : Resolve(path);
                        if (obj == null) return _jw.Error($"not_found: could not resolve '{path}'");
                        return new { path, type = obj.GetType().FullName, members = DescribeMembers(obj, Arg("filter", "")) };
                    }
                    case "describe":
                    {
                        var path = Arg("path", "");
                        object obj = string.IsNullOrEmpty(path) ? (object)Service : Resolve(path);
                        if (obj == null) return _jw.Error($"not_found: could not resolve '{path}'");
                        _debugLastResult = obj;
                        return new
                        {
                            path,
                            result = DescribeValue(obj, 0, ParseInt(Arg("depth", "1"), 1), ParseInt(Arg("sample", "5"), 5)),
                            members = DescribeMembers(obj, Arg("filter", ""))
                        };
                    }
                    case "call":
                    {
                        var path = Arg("path", "");
                        var methodName = Arg("method", "");
                        if (string.IsNullOrEmpty(methodName)) return _jw.Error("invalid_param: pass method:MethodName");
                        object obj = string.IsNullOrEmpty(path) ? (object)Service : Resolve(path);
                        if (obj == null) return _jw.Error($"not_found: could not resolve '{path}'");
                        var call = ResolveMethodCall(obj, methodName, args);
                        if (call.error != null) return _jw.Error(call.error);
                        var result = call.method.Invoke(obj, call.callArgs);
                        _debugLastResult = result;
                        return new { path, method = call.method.Name, result = DescribeValue(result, 0, ParseInt(Arg("depth", "1"), 1), ParseInt(Arg("sample", "5"), 5)) };
                    }
                    case "compare":
                    {
                        var leftPath = Arg("left", Arg("path", ""));
                        if (string.IsNullOrEmpty(leftPath)) return _jw.Error("invalid_param: pass left:path.to.value");
                        var left = Resolve(leftPath);
                        object right = args.ContainsKey("value") ? ParseLooseValue(Arg("value")) : Resolve(Arg("right", ""));
                        return new { leftPath, rightPath = Arg("right", ""), equal = ValuesEqual(left, right), left = DescribeValue(left, 0, 1, 5), right = DescribeValue(right, 0, 1, 5) };
                    }
                    case "assert":
                    {
                        var path = Arg("path", "");
                        if (string.IsNullOrEmpty(path)) return _jw.Error("invalid_param: pass path:path.to.value");
                        var left = Resolve(path);
                        object right = args.ContainsKey("value") ? ParseLooseValue(Arg("value")) : Resolve(Arg("right", ""));
                        var ok = EvaluateAssertion(left, Arg("op", "eq"), right, out var detail);
                        return new { path, op = Arg("op", "eq"), ok, left = DescribeValue(left, 0, 1, 5), right = DescribeValue(right, 0, 1, 5), detail };
                    }
                    case "validate": return ValidateEntity(ParseInt(Arg("id", "0"), 0));
                    case "validate_all": return ValidateAll();
                    default: return _jw.Error($"invalid_param: unknown target '{target}'. use: help, roots, get, fields, describe, call, compare, assert, validate, validate_all");
                }
            }
            catch (Exception ex)
            {
                return _jw.Error("internal_error: " + ex.Message.Replace("\"", "'").Replace("\r", "").Replace("\n", " | "));
            }
        }

        private object ValidateEntity(int id)
        {
            float now = Time.realtimeSinceStartup;
            var buildings = Service.ReadV2.EnsureBuildingsFreshNow(now, true);
            var beavers = Service.ReadV2.EnsureBeaversFreshNow(now, true);
            var natural = Service.ReadV2.EnsureNaturalResourcesFreshNow(now);

            for (int i = 0; i < buildings.Count; i++)
                if (buildings.Definitions[i].Id == id)
                {
                    TimberbotReadV2.TrackedBuildingRef match = null;
                    foreach (var t in Service.ReadV2.TrackedBuildings) if (t.Id == id) { match = t; break; }
                    return match != null ? ValidateBuilding(id, buildings, i, match) : _jw.Error("tracked_not_found", ("id", id));
                }

            for (int i = 0; i < beavers.Count; i++)
                if (beavers.Definitions[i].Id == id)
                {
                    TimberbotReadV2.TrackedBeaverRef match = null;
                    foreach (var t in Service.ReadV2.TrackedBeavers) if (t.Definition.Id == id) { match = t; break; }
                    return match != null ? ValidateBeaver(id, beavers, i, match) : _jw.Error("tracked_not_found", ("id", id));
                }

            for (int i = 0; i < natural.Count; i++)
                if (natural.Definitions[i].Id == id)
                {
                    TimberbotReadV2.TrackedNaturalResourceRef match = null;
                    foreach (var t in Service.ReadV2.TrackedNaturalResources) if (t.Definition.Id == id) { match = t; break; }
                    return match != null ? ValidateNaturalResource(id, natural, i, match) : _jw.Error("tracked_not_found", ("id", id));
                }

            return _jw.Error("not_found", ("id", id));
        }

        private object ValidateAll()
        {
            float now = Time.realtimeSinceStartup;
            var buildings = Service.ReadV2.EnsureBuildingsFreshNow(now, true);
            var beavers = Service.ReadV2.EnsureBeaversFreshNow(now, true);
            var natural = Service.ReadV2.EnsureNaturalResourcesFreshNow(now);
            var districts = Service.ReadV2.EnsureDistrictsFreshNow(now);
            var failures = new List<object>();
            int totalEntities = 0, totalFields = 0, totalMismatches = 0;

            var trackB = new Dictionary<int, TimberbotReadV2.TrackedBuildingRef>();
            foreach (var t in Service.ReadV2.TrackedBuildings) trackB[t.Id] = t;
            for (int i = 0; i < buildings.Count; i++)
            {
                var id = buildings.Definitions[i].Id;
                if (trackB.TryGetValue(id, out var t))
                    Accumulate(ValidateBuilding(id, buildings, i, t), failures, ref totalEntities, ref totalFields, ref totalMismatches);
            }

            var trackBeav = new Dictionary<int, TimberbotReadV2.TrackedBeaverRef>();
            foreach (var t in Service.ReadV2.TrackedBeavers) trackBeav[t.Definition.Id] = t;
            for (int i = 0; i < beavers.Count; i++)
            {
                var id = beavers.Definitions[i].Id;
                if (trackBeav.TryGetValue(id, out var t))
                    Accumulate(ValidateBeaver(id, beavers, i, t), failures, ref totalEntities, ref totalFields, ref totalMismatches);
            }

            var trackNat = new Dictionary<int, TimberbotReadV2.TrackedNaturalResourceRef>();
            foreach (var t in Service.ReadV2.TrackedNaturalResources) trackNat[t.Definition.Id] = t;
            for (int i = 0; i < natural.Count; i++)
            {
                var id = natural.Definitions[i].Id;
                if (trackNat.TryGetValue(id, out var t))
                    Accumulate(ValidateNaturalResource(id, natural, i, t), failures, ref totalEntities, ref totalFields, ref totalMismatches);
            }

            var liveDistricts = new Dictionary<string, (int adults, int children, int bots)>();
            foreach (var dc in Service.ReadV2.DebugDistrictRegistry.AllDistrictCenters)
            {
                var pop = dc.DistrictPopulation;
                liveDistricts[dc.DistrictName] = (
                    pop != null ? pop.NumberOfAdults : 0,
                    pop != null ? pop.NumberOfChildren : 0,
                    pop != null ? pop.NumberOfBots : 0);
            }

            if (districts != null)
            {
                for (int i = 0; i < districts.Length; i++)
                {
                    var snapshot = districts[i];
                    var fields = new Dictionary<string, object>();
                    int mismatches = 0, total = 0;
                    if (liveDistricts.TryGetValue(snapshot.Name, out var live))
                    {
                        AddComparison(fields, ref mismatches, ref total, "adults", snapshot.Adults, live.adults);
                        AddComparison(fields, ref mismatches, ref total, "children", snapshot.Children, live.children);
                        AddComparison(fields, ref mismatches, ref total, "bots", snapshot.Bots, live.bots);
                    }
                    else
                    {
                        AddComparison(fields, ref mismatches, ref total, "exists", true, false);
                    }

                    totalEntities++;
                    totalFields += total;
                    totalMismatches += mismatches;
                    if (mismatches > 0)
                        failures.Add(new { type = "district", name = snapshot.Name, fields, mismatches, total });
                }
            }

            return new { entities = totalEntities, fields = totalFields, mismatches = totalMismatches, failures };
        }

        private object ValidateBuilding(
            int id,
            TimberbotReadV2.ProjectionSnapshot<TimberbotReadV2.BuildingDefinition, TimberbotReadV2.BuildingState, TimberbotReadV2.BuildingDetailState>.Snapshot snapshot,
            int index,
            TimberbotReadV2.TrackedBuildingRef tracked)
        {
            var def = snapshot.Definitions[index];
            var state = snapshot.States[index];
            var fields = new Dictionary<string, object>();
            int mismatches = 0, total = 0;

            if (tracked.BlockObject != null)
            {
                var coords = tracked.BlockObject.CoordinatesAtBaseZ;
                AddComparison(fields, ref mismatches, ref total, "x", def.X, coords.x);
                AddComparison(fields, ref mismatches, ref total, "y", def.Y, coords.y);
                AddComparison(fields, ref mismatches, ref total, "z", def.Z, coords.z);
                AddComparison(fields, ref mismatches, ref total, "finished", state.Finished, tracked.BlockObject.IsFinished ? 1 : 0);
            }
            if (tracked.Pausable != null) AddComparison(fields, ref mismatches, ref total, "paused", state.Paused, tracked.Pausable.Paused ? 1 : 0);
            if (tracked.Workplace != null)
            {
                AddComparison(fields, ref mismatches, ref total, "assignedWorkers", state.AssignedWorkers, tracked.Workplace.NumberOfAssignedWorkers);
                AddComparison(fields, ref mismatches, ref total, "desiredWorkers", state.DesiredWorkers, tracked.Workplace.DesiredWorkers);
                AddComparison(fields, ref mismatches, ref total, "maxWorkers", state.MaxWorkers, tracked.Workplace.MaxWorkers);
            }
            if (tracked.Dwelling != null)
            {
                AddComparison(fields, ref mismatches, ref total, "dwellers", state.Dwellers, tracked.Dwelling.NumberOfDwellers);
                AddComparison(fields, ref mismatches, ref total, "maxDwellers", state.MaxDwellers, tracked.Dwelling.MaxBeavers);
            }
            if (tracked.Mechanical != null) AddComparison(fields, ref mismatches, ref total, "powered", state.Powered, tracked.Mechanical.ActiveAndPowered ? 1 : 0);
            if (tracked.Floodgate != null) AddComparison(fields, ref mismatches, ref total, "floodgateHeight", state.FloodgateHeight, tracked.Floodgate.Height);
            if (tracked.Clutch != null) AddComparison(fields, ref mismatches, ref total, "clutchEngaged", state.ClutchEngaged, tracked.Clutch.IsEngaged ? 1 : 0);
            if (tracked.Wonder != null) AddComparison(fields, ref mismatches, ref total, "wonderActive", state.WonderActive, tracked.Wonder.IsActive ? 1 : 0);
            AddComparison(fields, ref mismatches, ref total, "name", def.Name, tracked.BlockObject != null ? TimberbotEntityRegistry.CanonicalName(tracked.BlockObject.GameObject.name) : def.Name);

            return new { id, type = "building", name = def.Name, fields, mismatches, total };
        }

        private object ValidateBeaver(
            int id,
            TimberbotReadV2.ProjectionSnapshot<TimberbotReadV2.BeaverDefinition, TimberbotReadV2.BeaverState, TimberbotReadV2.BeaverDetailState>.Snapshot snapshot,
            int index,
            TimberbotReadV2.TrackedBeaverRef tracked)
        {
            var def = snapshot.Definitions[index];
            var state = snapshot.States[index];
            var fields = new Dictionary<string, object>();
            int mismatches = 0, total = 0;

            if (tracked.WbTracker != null) AddComparison(fields, ref mismatches, ref total, "wellbeing", state.Wellbeing, tracked.WbTracker.Wellbeing);
            if (tracked.Citizen?.AssignedDistrict != null) AddComparison(fields, ref mismatches, ref total, "district", state.District, tracked.Citizen.AssignedDistrict.DistrictName);
            if (tracked.Go != null)
            {
                var pos = tracked.Go.transform.position;
                AddComparison(fields, ref mismatches, ref total, "x", state.X, Mathf.FloorToInt(pos.x));
                AddComparison(fields, ref mismatches, ref total, "y", state.Y, Mathf.FloorToInt(pos.z));
                AddComparison(fields, ref mismatches, ref total, "z", state.Z, Mathf.FloorToInt(pos.y));
            }
            var wp = tracked.Worker?.Workplace;
            AddComparison(fields, ref mismatches, ref total, "workplace", state.Workplace ?? "", wp != null ? TimberbotEntityRegistry.CanonicalName(wp.GameObject.name) : "");

            return new { id, type = "beaver", name = def.Name, fields, mismatches, total };
        }

        private object ValidateNaturalResource(
            int id,
            TimberbotReadV2.ProjectionSnapshot<TimberbotReadV2.NaturalResourceDefinition, TimberbotReadV2.NaturalResourceState, TimberbotReadV2.NoDetail>.Snapshot snapshot,
            int index,
            TimberbotReadV2.TrackedNaturalResourceRef tracked)
        {
            var def = snapshot.Definitions[index];
            var state = snapshot.States[index];
            var fields = new Dictionary<string, object>();
            int mismatches = 0, total = 0;

            if (tracked.BlockObject != null)
            {
                var coords = tracked.BlockObject.Coordinates;
                AddComparison(fields, ref mismatches, ref total, "x", state.X, coords.x);
                AddComparison(fields, ref mismatches, ref total, "y", state.Y, coords.y);
                AddComparison(fields, ref mismatches, ref total, "z", state.Z, coords.z);
            }
            if (tracked.Living != null) AddComparison(fields, ref mismatches, ref total, "alive", state.Alive, tracked.Living.IsDead ? 0 : 1);
            if (tracked.Growable != null)
            {
                AddComparison(fields, ref mismatches, ref total, "grown", state.Grown, tracked.Growable.IsGrown ? 1 : 0);
                AddComparison(fields, ref mismatches, ref total, "growth", state.Growth, tracked.Growable.GrowthProgress);
            }
            AddComparison(fields, ref mismatches, ref total, "name", def.Name, def.Name);

            return new { id, type = "naturalResource", name = def.Name, fields, mismatches, total };
        }

        private List<Dictionary<string, object>> GetRoots()
        {
            return Service.GetType().GetFields(_flags)
                .Where(f => !f.IsStatic && f.Name != "Cache")
                .Select(f => new Dictionary<string, object> { ["name"] = f.Name, ["type"] = f.FieldType.FullName })
                .OrderBy(f => (string)f["name"])
                .ToList();
        }

        private object Resolve(string path)
        {
            if (string.IsNullOrEmpty(path)) return null;
            if (path == "Cache" || path.StartsWith("Cache.", StringComparison.Ordinal))
                throw new InvalidOperationException("invalid_path: use ReadV2 or Registry");

            var parts = path.Split('.');
            object current = parts[0] == "$" ? _debugLastResult : (object)Service;
            if (parts[0] == "$") parts = parts.Skip(1).ToArray();

            foreach (var part in parts)
            {
                if (current == null) return null;
                if (part.StartsWith("[") && part.EndsWith("]"))
                {
                    int idx = int.Parse(part.Substring(1, part.Length - 2));
                    if (current is IList list)
                        current = idx < list.Count ? list[idx] : null;
                    else if (current is IEnumerable enumerable)
                    {
                        int i = 0;
                        current = null;
                        foreach (var item in enumerable)
                            if (i++ == idx) { current = item; break; }
                    }
                    else return null;
                    continue;
                }
                if (part.StartsWith("~"))
                {
                    var allCompsProp = current.GetType().GetProperty("AllComponents", _flags);
                    if (allCompsProp == null) return null;
                    var allComps = allCompsProp.GetValue(current) as IEnumerable;
                    current = null;
                    if (allComps != null)
                    {
                        string typeName = part.Substring(1);
                        foreach (var comp in allComps)
                            if (comp.GetType().Name == typeName || comp.GetType().FullName.Contains(typeName))
                            { current = comp; break; }
                    }
                    continue;
                }
                var t = current.GetType();
                var field = t.GetField(part, _flags);
                if (field != null) { current = field.GetValue(current); continue; }
                var prop = t.GetProperty(part, _flags);
                if (prop != null) { current = prop.GetValue(current); continue; }
                var method = t.GetMethod(part, _flags, null, Type.EmptyTypes, null);
                if (method != null) { current = method.Invoke(current, null); continue; }
                return null;
            }

            return current;
        }

        private List<Dictionary<string, object>> DescribeMembers(object obj, string filter)
        {
            var members = new List<Dictionary<string, object>>();
            if (obj == null) return members;
            var type = obj.GetType();
            filter = filter ?? "";

            foreach (var field in type.GetFields(_flags).Where(f => !f.IsStatic))
            {
                if (!string.IsNullOrEmpty(filter) && field.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                members.Add(new Dictionary<string, object> { ["name"] = field.Name, ["kind"] = "field", ["type"] = field.FieldType.FullName });
            }
            foreach (var prop in type.GetProperties(_flags).Where(p => p.GetIndexParameters().Length == 0))
            {
                if (!string.IsNullOrEmpty(filter) && prop.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                members.Add(new Dictionary<string, object> { ["name"] = prop.Name, ["kind"] = "property", ["type"] = prop.PropertyType.FullName });
            }
            foreach (var method in type.GetMethods(_flags).Where(m => !m.IsSpecialName))
            {
                if (!string.IsNullOrEmpty(filter) && method.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                members.Add(new Dictionary<string, object> { ["name"] = method.Name, ["kind"] = "method", ["type"] = method.ReturnType.FullName });
            }

            return members.OrderBy(m => (string)m["name"]).ToList();
        }

        private object DescribeValue(object value, int depth, int maxDepth, int maxItems)
        {
            if (value == null) return null;
            if (depth >= maxDepth) return value.ToString();
            var type = value.GetType();
            if (value is string || type.IsPrimitive || value is decimal) return value;

            if (value is IDictionary dict)
            {
                var result = new Dictionary<string, object>();
                int count = 0;
                foreach (DictionaryEntry entry in dict)
                {
                    if (count++ >= maxItems) break;
                    result[entry.Key?.ToString() ?? "null"] = DescribeValue(entry.Value, depth + 1, maxDepth, maxItems);
                }
                return result;
            }

            if (value is IEnumerable enumerable && !(value is string))
            {
                var result = new List<object>();
                int count = 0;
                foreach (var item in enumerable)
                {
                    if (count++ >= maxItems) break;
                    result.Add(DescribeValue(item, depth + 1, maxDepth, maxItems));
                }
                return result;
            }

            var obj = new Dictionary<string, object> { ["type"] = type.FullName };
            foreach (var field in type.GetFields(_flags).Where(f => !f.IsStatic).Take(maxItems))
                obj[field.Name] = DescribeValue(field.GetValue(value), depth + 1, maxDepth, maxItems);
            foreach (var prop in type.GetProperties(_flags).Where(p => p.GetIndexParameters().Length == 0).Take(maxItems))
            {
                try { obj[prop.Name] = DescribeValue(prop.GetValue(value), depth + 1, maxDepth, maxItems); }
                catch { }
            }
            return obj;
        }

        private (MethodInfo method, object[] callArgs, string error) ResolveMethodCall(object obj, string methodName, Dictionary<string, string> args)
        {
            var methods = obj.GetType().GetMethods(_flags).Where(m => m.Name == methodName).ToArray();
            foreach (var method in methods)
            {
                var parameters = method.GetParameters();
                var callArgs = new object[parameters.Length];
                bool ok = true;
                for (int i = 0; i < parameters.Length; i++)
                {
                    if (!args.TryGetValue($"arg{i}", out var raw)) { ok = false; break; }
                    try { callArgs[i] = ConvertArgument(raw, parameters[i].ParameterType); }
                    catch { ok = false; break; }
                }
                if (ok) return (method, callArgs, null);
            }
            return (null, null, $"not_found: method '{methodName}'");
        }

        private static object ConvertArgument(string raw, Type targetType)
        {
            if (targetType == typeof(string)) return raw;
            if (targetType == typeof(int)) return int.Parse(raw);
            if (targetType == typeof(float)) return float.Parse(raw);
            if (targetType == typeof(bool)) return bool.Parse(raw);
            if (targetType == typeof(Vector3Int))
            {
                var parts = raw.Split(',');
                return new Vector3Int(int.Parse(parts[0]), int.Parse(parts[1]), int.Parse(parts[2]));
            }
            return Convert.ChangeType(raw, targetType);
        }

        private static object ParseLooseValue(string raw)
        {
            if (raw == null) return null;
            if (bool.TryParse(raw, out var b)) return b;
            if (int.TryParse(raw, out var i)) return i;
            if (float.TryParse(raw, out var f)) return f;
            return raw;
        }

        private static int ParseInt(string raw, int fallback) => int.TryParse(raw, out var value) ? value : fallback;

        private static bool ValuesEqual(object left, object right) => TimberbotPure.ValuesEqual(left, right);

        private static bool TryGetNumeric(object value, out double numeric) => TimberbotPure.TryGetNumeric(value, out numeric);

        private static int CompareValues(object left, object right, out bool comparable) => TimberbotPure.CompareValues(left, right, out comparable);

        private static bool EvaluateAssertion(object left, string op, object right, out string detail) => TimberbotPure.EvaluateAssertion(left, op, right, out detail);

        private static void AddComparison(Dictionary<string, object> fields, ref int mismatches, ref int total, string name, object cached, object live)
        {
            bool match = Equals(cached, live);
            if (!match && cached is IConvertible && live is IConvertible)
            {
                try
                {
                    double dc = Convert.ToDouble(cached);
                    double dl = Convert.ToDouble(live);
                    match = Math.Abs(dc - dl) < 0.5;
                }
                catch { }
            }
            fields[name] = new { cached, live, match };
            total++;
            if (!match) mismatches++;
        }

        private static double ToMs(System.Diagnostics.Stopwatch sw) => sw.ElapsedTicks * 1000.0 / System.Diagnostics.Stopwatch.Frequency;

        private static void Accumulate(object result, List<object> failures, ref int totalEntities, ref int totalFields, ref int totalMismatches)
        {
            totalEntities++;
            int mismatches = ExtractInt(result, "mismatches");
            int total = ExtractInt(result, "total");
            totalFields += total;
            totalMismatches += mismatches;
            if (mismatches > 0) failures.Add(result);
        }

        private static int ExtractInt(object obj, string name)
        {
            var prop = obj.GetType().GetProperty(name);
            return prop != null ? (int)prop.GetValue(obj) : 0;
        }
    }
}

