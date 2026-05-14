// TimberbotPlacement.cs. Building placement, path routing, terrain queries.
//
// FindPlacement: searches a region for valid building spots using the game's own
// validation (PreviewFactory.Create + BlockObject.IsValid). Checks flooding via
// WaterDepth on ground-required tiles, path connectivity via reflection into NavMesh
// internals, and power adjacency via cached power tile positions. Water buildings
// sort by waterDepth first. Others: non-flooded > reachable > distance (closer) > pathAccess > nearPower.
//
// PlaceBuilding: origin-corrects coordinates (user always specifies bottom-left),
// creates a preview, validates, then calls BlockObjectPlacerService.Place().
//
// RoutePathJob: plans and places paths incrementally under the write-job budget.
// Handles multi-level jumps by stacking platforms + stairs automatically.
//
// GetTerrainHeight: reads terrain column data for a single tile.

using System.Collections.Generic;
using Timberborn.BlockSystem;
using Timberborn.Buildings;
using Timberborn.BlockObjectTools;
using Timberborn.Coordinates;
using Timberborn.MapIndexSystem;
using Timberborn.TerrainSystem;
using Timberborn.WaterBuildings;
using Timberborn.WaterSystem;
using Timberborn.EntitySystem;
using Timberborn.GameDistricts;
using Timberborn.ScienceSystem;
using Timberborn.BuildingsNavigation;
using Timberborn.GameFactionSystem;
using Timberborn.NaturalResourcesLifecycle;
using UnityEngine;

namespace Timberbot
{
    public struct PlaceBuildingResult
    {
        public int Id;
        public string Name;
        public string Error;
        public string Prefab;
        public int ScienceCost;
        public int CurrentPoints;
        public string Occupant;
        public int X, Y, Z;
        public string Orientation;
        public bool Success => Id != 0;

        public static PlaceBuildingResult Fail(string error, int x = 0, int y = 0, int z = 0)
            => new PlaceBuildingResult { Error = error, X = x, Y = y, Z = z };

        public string ToJson(TimberbotJw jw)
        {
            jw.Reset().OpenObj();
            if (Error != null)
            {
                jw.Prop("error", Error);
                if (X != 0 || Y != 0) jw.Prop("x", X).Prop("y", Y).Prop("z", Z);
                if (Prefab != null) jw.Prop("prefab", Prefab);
                if (ScienceCost > 0) jw.Prop("scienceCost", ScienceCost).Prop("currentPoints", CurrentPoints);
                if (Occupant != null) jw.Prop("occupant", Occupant);
            }
            else
            {
                jw.Prop("id", Id).Prop("name", Name)
                  .Prop("x", X).Prop("y", Y).Prop("z", Z)
                  .Prop("orientation", Orientation);
            }
            return jw.CloseObj().ToString();
        }

        public void WriteErrorJson(TimberbotJw jw)
        {
            jw.OpenObj();
            if (Prefab != null) jw.Prop("prefab", Prefab);
            jw.Prop("error", Error ?? "unknown");
            if (ScienceCost > 0) jw.Prop("scienceCost", ScienceCost).Prop("currentPoints", CurrentPoints);
            jw.CloseObj();
        }
    }

    public class TimberbotPlacement
    {
        public readonly TimberbotJw Jw = new TimberbotJw(1024);
        
        public event System.Action<string> OnActionLog;
        private void PostLog(string message) => OnActionLog?.Invoke(message);
        private readonly ITerrainService _terrainService;
        private readonly IThreadSafeWaterMap _waterMap;
        private readonly MapIndexService _mapIndexService;
        private readonly IThreadSafeColumnTerrainMap _terrainMap;
        private readonly BuildingService _buildingService;
        private readonly BuildingUnlockingService _buildingUnlockingService;
        private readonly BlockObjectPlacerService _blockObjectPlacerService;
        private readonly EntityService _entityService;
        private readonly Timberborn.Navigation.INavMeshService _navMeshService;
        private readonly DistrictCenterRegistry _districtCenterRegistry;
        private readonly PreviewFactory _previewFactory;
        private readonly ScienceService _scienceService;
        private readonly FactionService _factionService;
        private readonly TimberbotReadV2 _readV2;
        private readonly TimberbotEntityRegistry _cache;

        public TimberbotPlacement(
            ITerrainService terrainService,
            IThreadSafeWaterMap waterMap,
            MapIndexService mapIndexService,
            IThreadSafeColumnTerrainMap terrainMap,
            BuildingService buildingService,
            BuildingUnlockingService buildingUnlockingService,
            BlockObjectPlacerService blockObjectPlacerService,
            EntityService entityService,
            Timberborn.Navigation.INavMeshService navMeshService,
            DistrictCenterRegistry districtCenterRegistry,
            PreviewFactory previewFactory,
            ScienceService scienceService,
            FactionService factionService,
            TimberbotReadV2 readV2,
            TimberbotEntityRegistry cache)
        {
            _terrainService = terrainService;
            _waterMap = waterMap;
            _mapIndexService = mapIndexService;
            _terrainMap = terrainMap;
            _buildingService = buildingService;
            _buildingUnlockingService = buildingUnlockingService;
            _blockObjectPlacerService = blockObjectPlacerService;
            _entityService = entityService;
            _navMeshService = navMeshService;
            _districtCenterRegistry = districtCenterRegistry;
            _previewFactory = previewFactory;
            _scienceService = scienceService;
            _factionService = factionService;
            _readV2 = readV2;
            _cache = cache;
        }

        private static readonly string[] OrientNames = TimberbotEntityRegistry.OrientNames;
        private static readonly string[] PriorityNames = TimberbotEntityRegistry.PriorityNames;
        private static string GetPriorityName(Timberborn.PrioritySystem.Priority p) => TimberbotEntityRegistry.GetPriorityName(p);

        // Faction suffix for prefab names (e.g. ".IronTeeth" or ".Folktails").
        // Detected once at startup via FactionService.Current.Id. the same API
        // other mods (UnifiedFactions) use. Faction never changes during a game session.
        private string _factionSuffix = "";

        // Called once from TimberbotService.Load(), before BuildAllIndexes.
        // Sets both the local suffix (for RoutePath prefabs) and the static suffix
        // on TimberbotEntityRegistry (for CleanName to strip faction from entity names).
        public void DetectFaction()
        {
            _factionSuffix = "." + _factionService.Current.Id;
            TimberbotEntityRegistry.FactionSuffix = _factionSuffix;
            TimberbotLog.Info($"faction: {_factionSuffix}");
        }

        // ================================================================

        // Read the terrain height at a single tile.
        // Timberborn stores terrain as columns: each (x,y) cell has N stacked terrain
        // segments (for caves, overhangs, etc). We want the ceiling of the topmost
        // segment, which is the surface height where buildings can sit.
        // ColumnCounts[index2D] = how many segments at this cell.
        // topIndex = base + (count-1) * stride = the last (topmost) segment.
        private int GetTerrainHeight(int x, int y)
        {
            var size = _terrainService.Size;
            if (x < 0 || x >= size.x || y < 0 || y >= size.y) return 0;
            var index2D = _mapIndexService.CellToIndex(new Vector2Int(x, y));
            var stride = _mapIndexService.VerticalStride;
            var columnCount = _terrainMap.ColumnCounts[index2D];
            if (columnCount <= 0) return 0;
            var topIndex = index2D + (columnCount - 1) * stride;
            return _terrainMap.GetColumnCeiling(topIndex);
        }

        // Check if a tile has an overhang (multiple terrain columns).
        // Multiple columns = cave/overhang = unsafe for path/stair placement.
        private bool HasOverhang(int x, int y)
        {
            var size = _terrainService.Size;
            if (x < 0 || x >= size.x || y < 0 || y >= size.y) return false;
            var index2D = _mapIndexService.CellToIndex(new Vector2Int(x, y));
            return _terrainMap.ColumnCounts[index2D] > 1;
        }

        // Read water depth at a tile from the water column data.
        // Iterates columns top-down, returns first with WaterDepth > 0.
        private float GetWaterDepth(int x, int y)
        {
            try
            {
                var idx2D = _mapIndexService.CellToIndex(new Vector2Int(x, y));
                int colCount = _waterMap.ColumnCount(idx2D);
                var stride = _mapIndexService.VerticalStride;
                for (int ci = colCount - 1; ci >= 0; ci--)
                {
                    int idx3D = ci * stride + idx2D;
                    var col = _waterMap.WaterColumns[idx3D];
                    if (col.WaterDepth > 0) return col.WaterDepth;
                }
            }
            catch (System.Exception _ex) { TimberbotLog.Error("water", _ex); }
            return 0f;
        }

        internal string InvalidPrefabError(string badName)
        {
            // find prefabs containing any part of the bad name (case-insensitive)
            var lower = badName.ToLowerInvariant();
            // strip faction suffix for matching: "WaterPump.Folktails" -> "WaterPump"
            var baseName = lower.Contains(".") ? lower.Substring(0, lower.IndexOf('.')) : lower;
            var matches = new System.Collections.Generic.List<string>();
            foreach (var b in _buildingService.Buildings)
            {
                var spec = b.GetSpec<Timberborn.TemplateSystem.TemplateSpec>();
                if (spec == null) continue;
                var name = spec.TemplateName;
                var nameLower = name.ToLowerInvariant();
                if (nameLower.Contains(baseName) || baseName.Contains(nameLower.Contains(".") ? nameLower.Substring(0, nameLower.IndexOf('.')) : nameLower))
                    matches.Add(name);
            }
            if (matches.Count == 0)
                return "invalid_prefab: '" + badName + "' not found. no similar prefabs. run: timberbot.py prefabs to list all";
            return "invalid_prefab: '" + badName + "' not found. similar: " + string.Join(", ", matches) + ". run: timberbot.py prefabs to list all";
        }

        // ================================================================
        // WRITE ENDPOINTS. Tier 3
        // ================================================================

        // List all building templates (prefabs) the faction can build.
        // BuildingService.Buildings iterates the registered template definitions, not
        // placed buildings. Each template has a TemplateSpec (name), BlockObjectSpec
        // (footprint size), and BuildingSpec (science cost, material cost).
        //
        // BuildingCost uses reflection because the property names changed between
        // Timberborn versions (GoodId vs Id). Collect-then-emit pattern on costs
        // prevents partial JSON if reflection throws mid-iteration.
        public object CollectPrefabs()
        {
            var jw = Jw.Reset().BeginArr();
            foreach (var building in _buildingService.Buildings)
            {
                var templateSpec = building.GetSpec<Timberborn.TemplateSystem.TemplateSpec>();
                var blockSpec = building.GetSpec<BlockObjectSpec>();
                jw.OpenObj().Prop("name", templateSpec?.TemplateName ?? "unknown");
                if (blockSpec != null)
                {
                    var size = blockSpec.Size;
                    jw.Prop("sizeX", size.x).Prop("sizeY", size.y).Prop("sizeZ", size.z);
                }
                var bs = building.GetSpec<BuildingSpec>();
                if (bs != null)
                {
                    if (bs.ScienceCost > 0)
                        jw.Prop("scienceCost", bs.ScienceCost).Prop("unlocked", _buildingUnlockingService.Unlocked(bs));
                    // collect costs into a temp list, then write to JSON.
                    // if reflection fails mid-iteration, the JW state is still clean
                    try
                    {
                        var costs = new List<(string good, int amount)>();
                        foreach (var ga in bs.BuildingCost)
                        {
                            // reflection: try GoodId first (newer API), fall back to Id (older)
                            var goodProp = ga.GetType().GetProperty("GoodId") ?? ga.GetType().GetProperty("Id");
                            var amtProp = ga.GetType().GetProperty("Amount");
                            if (goodProp != null && amtProp != null)
                                costs.Add((goodProp.GetValue(ga)?.ToString(), (int)amtProp.GetValue(ga)));
                        }
                        if (costs.Count > 0)
                        {
                            jw.Arr("cost");
                            for (int ci = 0; ci < costs.Count; ci++)
                                jw.OpenObj().Prop("good", costs[ci].good).Prop("amount", costs[ci].amount).CloseObj();
                            jw.CloseArr();
                        }
                    }
                    catch (System.Exception _ex) { TimberbotLog.Error("prefabs.cost", _ex); }
                }
                jw.CloseObj();
            }
            return jw.End();
        }

        private object DemolishEntityById(int id, System.Func<EntityComponent, string> validateError)
        {
            var ec = _cache.FindEntity(id);
            if (ec == null)
                return Jw.Error("not_found: no entity with this id.", ("id", id));

            var validationError = validateError?.Invoke(ec);
            if (validationError != null)
                return Jw.Error(validationError, ("id", id), ("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)));

            var name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
            _entityService.Delete(ec);
            _readV2.InvalidateBuildings();
            _readV2.InvalidateNaturalResources();
            _readV2.InvalidateBeavers();
            PostLog($"Demolished {name} (ID:{id})");
            return Jw.Result(("id", id), ("name", name), ("demolished", true));
        }

        // remove a building from the world
        public object DemolishBuilding(int buildingId)
            => DemolishEntityById(buildingId, _ => null);

        public object DemolishCrop(int cropId)
            => DemolishEntityById(cropId, ec =>
            {
                if (ec.GetComponent<LivingNaturalResource>() == null)
                    return "invalid_type: not a natural resource";
                var name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
                return TimberbotEntityRegistry.CropSpecies.Contains(name) ? null : "invalid_type: not a crop";
            });

        // =====================================================================
        // A* PATH ROUTING
        // =====================================================================
        // Routes a path from (x1,y1) to (x2,y2) across arbitrary terrain,
        // automatically placing stairs at z-level changes and platforms for
        // multi-level jumps. The algorithm:
        //
        // 1. BUILD SURFACE GRAPH: scan terrain in the bounding box, create a
        //    node for each walkable surface tile (x,y,z). Add edges between
        //    adjacent nodes at the same z. Add directed connector edges for
        //    existing stairs/platforms and potential new stair placements.
        //    Cost grid: water=expensive, existing paths=cheap, impassable=255.
        //
        // 2. A* SEARCH: find shortest path through the graph from start to goal.
        //    The graph is 3D (nodes at different z-levels), so the pathfinder
        //    naturally routes up/down via stair connector edges.
        //
        // 3. PLACEMENT: walk the A* path, place paths on flat segments, place
        //    platforms+stairs at vertical connector edges. Uses the game's own
        //    placement validation (PlaceBuilding) so invalid spots are skipped.
        //
        // The key insight: stairs in Timberborn are placed on the LOWER tile and
        // face a direction. A stair at (x,y,z) facing north connects z to z+1 on
        // the y+1 side. Platforms stack under stairs for multi-level jumps.
        private sealed class PlanningBuilding
        {
            public string Name;
            public int X;
            public int Y;
            public int Z;
            public string Orientation;
            public List<(int x, int y, int z)> OccupiedTiles;
        }

        private sealed class PathPlanningData
        {
            public readonly List<PlanningBuilding> Buildings = new List<PlanningBuilding>();
            public readonly List<(int x, int y)> NaturalTiles = new List<(int x, int y)>();
            public readonly List<(int x, int y)> BlockerTiles = new List<(int x, int y)>();
            public double SnapshotMs;
            public string StairsPrefab;
            public string PlatformPrefab;
            public bool StairsUnlocked;
            public bool PlatformUnlocked;
            public int MinX;
            public int MinY;
            public int MaxX;
            public int MaxY;
            public int GridWidth;
            public int GridHeight;
        }

        private sealed class PlacementAction
        {
            public string Prefab;
            public int X;
            public int Y;
            public int Z;
            public string Orientation;
            public string Bucket;
            public int StopX;
            public int StopY;
        }

        private sealed class PathPlanResult
        {
            public readonly List<PlacementAction> Actions = new List<PlacementAction>();
            public readonly List<string> Errors = new List<string>();
            public int ConnectorCount;
            public int GraphNodes;
            public int PathNodes;
            public int PathEdges;
            public double SnapshotMs;
            public double GraphMs;
            public double AstarMs;
            public int StoppedX;
            public int StoppedY;
            public bool Stopped;
        }

        internal ITimberbotWriteJob CreateRoutePathJob(int x1, int y1, int x2, int y2, string style = "direct", int sections = 0, bool timings = false, long queuedAtTicks = 0, int queuedAtFrame = 0)
            => new RoutePathJob(this, x1, y1, x2, y2, style, sections, timings, queuedAtTicks, queuedAtFrame);

        internal ITimberbotWriteJob CreateFindPlacementJob(string prefabName, int x1, int y1, int x2, int y2, string format = "toon")
            => new FindPlacementJob(this, prefabName, x1, y1, x2, y2, format);

        private PathPlanningData CapturePathPlanningData(int x1, int y1, int x2, int y2, string style)
        {
            const int PADDING = 10;
            int minX = System.Math.Min(x1, x2) - PADDING;
            int minY = System.Math.Min(y1, y2) - PADDING;
            int maxX = System.Math.Max(x1, x2) + PADDING;
            int maxY = System.Math.Max(y1, y2) + PADDING;
            var mapSize = _terrainService.Size;
            if (minX < 0) minX = 0;
            if (minY < 0) minY = 0;
            if (maxX >= mapSize.x) maxX = mapSize.x - 1;
            if (maxY >= mapSize.y) maxY = mapSize.y - 1;

            var data = new PathPlanningData
            {
                MinX = minX,
                MinY = minY,
                MaxX = maxX,
                MaxY = maxY,
                GridWidth = maxX - minX + 1,
                GridHeight = maxY - minY + 1,
                StairsPrefab = "Stairs" + _factionSuffix,
                PlatformPrefab = "Platform" + _factionSuffix
            };

            var snapshotSw = System.Diagnostics.Stopwatch.StartNew();
            var buildingSnapshot = _readV2.EnsureBuildingsFreshNow(Time.realtimeSinceStartup);
            snapshotSw.Stop();
            data.SnapshotMs += snapshotSw.Elapsed.TotalMilliseconds;
            snapshotSw.Restart();
            var naturalSnapshot = _readV2.EnsureNaturalResourcesFreshNow(Time.realtimeSinceStartup);
            snapshotSw.Stop();
            data.SnapshotMs += snapshotSw.Elapsed.TotalMilliseconds;

            var stairsSpec = _buildingService.GetBuildingTemplate(data.StairsPrefab);
            var platformSpec = _buildingService.GetBuildingTemplate(data.PlatformPrefab);
            var stairsBs = stairsSpec?.GetSpec<BuildingSpec>();
            var platformBs = platformSpec?.GetSpec<BuildingSpec>();
            data.StairsUnlocked = stairsBs == null || stairsBs.ScienceCost <= 0 || _buildingUnlockingService.Unlocked(stairsBs);
            data.PlatformUnlocked = platformBs == null || platformBs.ScienceCost <= 0 || _buildingUnlockingService.Unlocked(platformBs);

            bool IntersectsRegion(List<(int x, int y, int z)> tiles)
            {
                for (int i = 0; i < tiles.Count; i++)
                {
                    var t = tiles[i];
                    if (t.x >= minX && t.x <= maxX && t.y >= minY && t.y <= maxY)
                        return true;
                }
                return false;
            }

            for (int i = 0; i < buildingSnapshot.Count; i++)
            {
                var def = buildingSnapshot.Definitions[i];
                if (def.OccupiedTiles == null || def.Name == null) continue;
                var tiles = new List<(int x, int y, int z)>(def.OccupiedTiles.Length);
                foreach (var t in def.OccupiedTiles)
                    tiles.Add((t.x, t.y, t.z));
                if (!IntersectsRegion(tiles) &&
                    !(def.X >= minX && def.X <= maxX && def.Y >= minY && def.Y <= maxY))
                    continue;
                data.Buildings.Add(new PlanningBuilding
                {
                    Name = def.Name,
                    X = def.X,
                    Y = def.Y,
                    Z = def.Z,
                    Orientation = def.Orientation,
                    OccupiedTiles = tiles
                });
            }

            for (int i = 0; i < naturalSnapshot.Count; i++)
            {
                var nr = naturalSnapshot.States[i];
                if (nr.X < minX || nr.X > maxX || nr.Y < minY || nr.Y > maxY) continue;
                data.NaturalTiles.Add((nr.X, nr.Y));
            }

            var blockers = _readV2.TrackedBlockers;
            for (int i = 0; i < blockers.Count; i++)
            {
                var b = blockers[i];
                if (b.OccupiedTiles == null) continue;
                foreach (var tile in b.OccupiedTiles)
                {
                    if (tile.x < minX || tile.x > maxX || tile.y < minY || tile.y > maxY) continue;
                    data.BlockerTiles.Add((tile.x, tile.y));
                }
            }

            return data;
        }

        private PathPlanResult PlanRoute(PathPlanningData planning, int x1, int y1, int x2, int y2, string style, int sections)
        {
            if (style != "direct" && style != "straight") style = "direct";

            List<SurfaceNode> nodes;
            List<GraphEdge>[] adj;
            Dictionary<long, List<int>> nodesByTile;
            int connectorCount;
            var graphSw = System.Diagnostics.Stopwatch.StartNew();
            BuildSurfaceGraph(planning, out nodes, out adj, out nodesByTile, out connectorCount);
            graphSw.Stop();

            var result = new PathPlanResult
            {
                SnapshotMs = planning.SnapshotMs,
                GraphMs = graphSw.Elapsed.TotalMilliseconds,
                ConnectorCount = connectorCount,
                GraphNodes = nodes.Count,
                StoppedX = x2,
                StoppedY = y2
            };

            string PathKey(int x, int y, int z) => $"P|{x}|{y}|{z}";
            string PlatformKey(int x, int y, int z) => $"F|{x}|{y}|{z}";
            string StairKey(int x, int y, int z, int orient) => $"S|{x}|{y}|{z}|{orient}";

            var existingPathKeys = new HashSet<string>();
            var existingPlatformKeys = new HashSet<string>();
            var existingStairKeys = new HashSet<string>();
            for (int i = 0; i < planning.Buildings.Count; i++)
            {
                var cb = planning.Buildings[i];
                bool isPath = cb.Name.Contains("Path");
                bool isStairs = cb.Name.Contains("Stairs");
                bool isPlatform = cb.Name.Contains("Platform");
                if (!isPath && !isStairs && !isPlatform) continue;
                for (int t = 0; t < cb.OccupiedTiles.Count; t++)
                {
                    var tile = cb.OccupiedTiles[t];
                    if (isPath) existingPathKeys.Add(PathKey(tile.x, tile.y, tile.z));
                    if (isPlatform) existingPlatformKeys.Add(PlatformKey(tile.x, tile.y, tile.z));
                }
                if (isStairs)
                    existingStairKeys.Add(StairKey(cb.X, cb.Y, cb.Z, ParseOrientation(cb.Orientation ?? "south")));
            }

            List<int> startIds;
            List<int> goalIds;
            if (!nodesByTile.TryGetValue(TileKey(x1, y1), out startIds) || startIds.Count == 0)
                startIds = null;
            if (!nodesByTile.TryGetValue(TileKey(x2, y2), out goalIds) || goalIds.Count == 0)
                goalIds = null;

            if (startIds == null || goalIds == null)
            {
                if (startIds == null) result.Errors.Add($"no walkable surface at start ({x1},{y1})");
                if (goalIds == null) result.Errors.Add($"no walkable surface at goal ({x2},{y2})");
                return result;
            }

            var astarSw = System.Diagnostics.Stopwatch.StartNew();
            var path = AStarPath(nodes, adj, startIds, goalIds, nodes.Count * 8, style);
            astarSw.Stop();
            result.AstarMs = astarSw.Elapsed.TotalMilliseconds;
            if (path == null)
            {
                result.Errors.Add($"A* found no route from ({x1},{y1}) to ({x2},{y2}). {connectorCount} connectors in graph");
                return result;
            }

            result.PathNodes = path.Nodes.Count;
            result.PathEdges = path.Edges.Count;

            var plannedPathKeys = new HashSet<string>();
            var plannedPlatformKeys = new HashSet<string>();
            var plannedStairKeys = new HashSet<string>();

            void AddPathAction(SurfaceNode node, int stopX, int stopY)
            {
                if (!node.PlacePath || node.Z <= 0) return;
                string key = PathKey(node.X, node.Y, node.Z);
                if (existingPathKeys.Contains(key) || !plannedPathKeys.Add(key)) return;
                result.Actions.Add(new PlacementAction
                {
                    Prefab = "Path",
                    X = node.X,
                    Y = node.Y,
                    Z = node.Z,
                    Orientation = "south",
                    Bucket = "path",
                    StopX = stopX,
                    StopY = stopY
                });
            }

            if (path.Nodes.Count > 0)
                AddPathAction(nodes[path.Nodes[0]], x2, y2);

            int connectorCrossings = 0;
            for (int i = 0; i < path.Edges.Count; i++)
            {
                var edge = path.Edges[i];
                var src = nodes[path.Nodes[i]];
                var dst = nodes[path.Nodes[i + 1]];

                if (edge.IsConnector)
                {
                    if (edge.RequiresPlacement)
                    {
                        if (!planning.StairsUnlocked)
                        {
                            result.Errors.Add($"stairs not unlocked at ({src.X},{src.Y})");
                            result.Stopped = true;
                            result.StoppedX = src.X;
                            result.StoppedY = src.Y;
                            break;
                        }
                        if (edge.Levels > 1 && !planning.PlatformUnlocked)
                        {
                            result.Errors.Add($"platforms not unlocked for {edge.Levels}-level connector at ({src.X},{src.Y})");
                            result.Stopped = true;
                            result.StoppedX = src.X;
                            result.StoppedY = src.Y;
                            break;
                        }

                        for (int rtIdx = 0; rtIdx < edge.RampTiles.Count; rtIdx++)
                        {
                            var rt = edge.RampTiles[rtIdx];
                            for (int p = 0; p < rt.platCount; p++)
                            {
                                int platformZ = rt.baseZ + p;
                                string pk = PlatformKey(rt.x, rt.y, platformZ);
                                if (existingPlatformKeys.Contains(pk) || !plannedPlatformKeys.Add(pk)) continue;
                                result.Actions.Add(new PlacementAction
                                {
                                    Prefab = planning.PlatformPrefab,
                                    X = rt.x,
                                    Y = rt.y,
                                    Z = platformZ,
                                    Orientation = "south",
                                    Bucket = "platform",
                                    StopX = src.X,
                                    StopY = src.Y
                                });
                            }

                            int stairZ = rt.baseZ + rt.platCount;
                            string sk = StairKey(rt.x, rt.y, stairZ, edge.OrientIdx);
                            if (existingStairKeys.Contains(sk) || !plannedStairKeys.Add(sk)) continue;
                            result.Actions.Add(new PlacementAction
                            {
                                Prefab = planning.StairsPrefab,
                                X = rt.x,
                                Y = rt.y,
                                Z = stairZ,
                                Orientation = OrientNames[edge.OrientIdx],
                                Bucket = "stair",
                                StopX = src.X,
                                StopY = src.Y
                            });
                        }
                    }

                    AddPathAction(dst, dst.X, dst.Y);
                    connectorCrossings++;
                    if (sections > 0 && connectorCrossings >= sections)
                    {
                        result.Stopped = true;
                        result.StoppedX = dst.X;
                        result.StoppedY = dst.Y;
                        break;
                    }
                    continue;
                }

                AddPathAction(dst, dst.X, dst.Y);
            }

            return result;
        }

        private enum SurfaceKind : byte
        {
            Terrain,
            ExistingPath,
            ExistingPlatformTop
        }

        private struct SurfaceNode
        {
            public int X, Y, Z;
            public ushort EntryCost;
            public SurfaceKind Kind;
            public bool Existing;
            public bool PlacePath;
        }

        private struct GraphEdge
        {
            public int ToId;
            public ushort Cost;
            public bool IsConnector;
            public bool RequiresPlacement;
            public int BaseZ, Levels;
            public int EntX, EntY, EntZ, ExtX, ExtY, ExtZ;
            public int OrientIdx;
            public List<(int x, int y, int baseZ, int platCount)> RampTiles;
        }

        private sealed class SurfacePath
        {
            public List<int> Nodes;
            public List<GraphEdge> Edges;
        }

        private sealed class RoutePathJob : ITimberbotWriteJob
        {
            private readonly TimberbotPlacement _owner;
            private readonly int _x1;
            private readonly int _y1;
            private readonly int _x2;
            private readonly int _y2;
            private readonly string _style;
            private readonly int _sections;
            private readonly bool _timings;
            private readonly long _queuedAtTicks;
            private readonly int _queuedAtFrame;
            private readonly System.Diagnostics.Stopwatch _wallClock = System.Diagnostics.Stopwatch.StartNew();
            private System.Threading.Tasks.Task<PathPlanResult> _planningTask;
            private PathPlanResult _plan;
            private bool _completed;
            private int _statusCode = 200;
            private object _result;
            private int _framesActive;
            private int _actionIndex;
            private int _pathCount;
            private int _stairCount;
            private int _platformCount;
            private int _skipped;
            private int _placementsAttempted;
            private double _applyPathMs;
            private double _applyPlatformMs;
            private double _applyStairMs;
            private readonly List<PlaceBuildingResult> _failedResults = new List<PlaceBuildingResult>();
            private readonly List<string> _errors = new List<string>();
            private int _settleFramesRemaining = -1;
            private bool _awaitingFinalize;
            private int _startedFrame = -1;

            public RoutePathJob(TimberbotPlacement owner, int x1, int y1, int x2, int y2, string style, int sections, bool timings, long queuedAtTicks, int queuedAtFrame)
            {
                _owner = owner;
                _x1 = x1;
                _y1 = y1;
                _x2 = x2;
                _y2 = y2;
                _style = style;
                _sections = sections;
                _timings = timings;
                _queuedAtTicks = queuedAtTicks;
                _queuedAtFrame = queuedAtFrame;
            }

            public string Name => "path.place";
            public bool IsCompleted => _completed;
            public int StatusCode => _statusCode;
            public object Result => _result;

            public void Step(float now, double budgetMs)
            {
                if (_completed) return;
                if (_startedFrame < 0)
                    _startedFrame = UnityEngine.Time.frameCount;
                _framesActive++;

                if (_planningTask == null)
                {
                    var planningData = _owner.CapturePathPlanningData(_x1, _y1, _x2, _y2, _style);
                    _planningTask = System.Threading.Tasks.Task.Run(() => _owner.PlanRoute(planningData, _x1, _y1, _x2, _y2, _style, _sections));
                    return;
                }

                if (_plan == null)
                {
                    if (!_planningTask.IsCompleted)
                        return;

                    if (_planningTask.IsFaulted)
                    {
                        var ex = _planningTask.Exception?.GetBaseException();
                        FinishError("operation_failed: " + (ex?.Message ?? "route planning failed"));
                        return;
                    }

                    if (_planningTask.IsCanceled)
                    {
                        FinishError("operation_failed: route planning canceled");
                        return;
                    }

                    _plan = _planningTask.Result;
                    _errors.AddRange(_plan.Errors);
                    if (_plan.Actions.Count == 0)
                    {
                        FinishResponse();
                        return;
                    }
                }

                if (_awaitingFinalize)
                {
                    if (_settleFramesRemaining < 0)
                    {
                        _settleFramesRemaining = 1;
                        return;
                    }
                    if (_settleFramesRemaining > 0)
                    {
                        _settleFramesRemaining--;
                        return;
                    }
                    FinishResponse();
                    return;
                }

                var budget = System.Diagnostics.Stopwatch.StartNew();
                while (_actionIndex < _plan.Actions.Count)
                {
                    if (budget.Elapsed.TotalMilliseconds >= budgetMs)
                        return;

                    var action = _plan.Actions[_actionIndex];
                    _placementsAttempted++;
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    var placement = _owner.PlaceBuilding(action.Prefab, action.X, action.Y, action.Z, action.Orientation);
                    sw.Stop();

                    switch (action.Bucket)
                    {
                        case "path": _applyPathMs += sw.Elapsed.TotalMilliseconds; break;
                        case "platform": _applyPlatformMs += sw.Elapsed.TotalMilliseconds; break;
                        case "stair": _applyStairMs += sw.Elapsed.TotalMilliseconds; break;
                    }

                    if (placement.Success)
                    {
                        if (action.Bucket == "path") _pathCount++;
                        else if (action.Bucket == "platform") _platformCount++;
                        else if (action.Bucket == "stair") _stairCount++;
                    }
                    else if (!IsBenignOccupied(placement))
                    {
                        _skipped++;
                        _failedResults.Add(placement);
                        _errors.Add($"{action.Bucket} failed at ({action.X},{action.Y},{action.Z})");
                        _plan.Stopped = true;
                        _plan.StoppedX = action.StopX;
                        _plan.StoppedY = action.StopY;
                        _awaitingFinalize = true;
                        return;
                    }

                    _actionIndex++;
                }

                _awaitingFinalize = true;
            }

            public void Cancel(string error)
            {
                if (_completed) return;
                FinishError(error);
            }

            private static bool IsBenignOccupied(PlaceBuildingResult r) => r.Error != null && r.Error.StartsWith("occupied by");

            private void FinishError(string error)
            {
                _statusCode = 500;
                _result = _owner.Jw.Error(error);
                _completed = true;
            }

            private void FinishResponse()
            {
                var queueWaitMs = _queuedAtTicks > 0
                    ? (System.Diagnostics.Stopwatch.GetTimestamp() - _queuedAtTicks) * 1000d / System.Diagnostics.Stopwatch.Frequency - _wallClock.Elapsed.TotalMilliseconds
                    : 0d;
                if (queueWaitMs < 0d) queueWaitMs = 0d;

                var jw = _owner.Jw.Reset().BeginObj();
                jw.Obj("placed").Prop("paths", _pathCount);
                if (_stairCount > 0) jw.Prop("stairs", _stairCount);
                if (_platformCount > 0) jw.Prop("platforms", _platformCount);
                jw.CloseObj().Prop("skipped", _skipped);
                jw.Prop("stairEdgesInGrid", _plan?.ConnectorCount ?? 0);
                jw.Prop("connectorEdgesInGrid", _plan?.ConnectorCount ?? 0);
                if (_plan != null && _plan.Stopped)
                    jw.Prop("stoppedAt", $"{_plan.StoppedX},{_plan.StoppedY}");

                if (_failedResults.Count > 0 || _errors.Count > 0)
                {
                    jw.Arr("errors");
                    foreach (var r in _failedResults)
                        r.WriteErrorJson(jw);
                    foreach (var e in _errors)
                        jw.OpenObj().Prop("error", e).CloseObj();
                    jw.CloseArr();
                }
                if (_timings)
                {
                    int queuedFrames = _startedFrame >= 0 ? _startedFrame - _queuedAtFrame : 0;
                    if (queuedFrames < 0) queuedFrames = 0;
                    jw.Obj("timings")
                        .Prop("totalMs", (float)_wallClock.Elapsed.TotalMilliseconds, "F3")
                        .Prop("queueWaitMs", (float)queueWaitMs, "F3")
                        .Prop("snapshotMs", (float)(_plan?.SnapshotMs ?? 0d), "F3")
                        .Prop("graphMs", (float)(_plan?.GraphMs ?? 0d), "F3")
                        .Prop("astarMs", (float)(_plan?.AstarMs ?? 0d), "F3")
                        .Prop("applyPathMs", (float)_applyPathMs, "F3")
                        .Prop("applyPlatformMs", (float)_applyPlatformMs, "F3")
                        .Prop("applyStairMs", (float)_applyStairMs, "F3")
                        .Prop("placementMs", (float)(_applyPathMs + _applyPlatformMs + _applyStairMs), "F3")
                        .Prop("placementsAttempted", _placementsAttempted)
                        .Prop("graphNodes", _plan?.GraphNodes ?? 0)
                        .Prop("pathNodes", _plan?.PathNodes ?? 0)
                        .Prop("pathEdges", _plan?.PathEdges ?? 0)
                        .Prop("framesQueued", queuedFrames)
                        .Prop("framesActive", _framesActive)
                        .CloseObj();
                }
                jw.CloseObj();
                _result = jw.ToString();
                _completed = true;
            }
        }

        private static long TileKey(int x, int y)
        {
            return ((long)x << 20) ^ (uint)y;
        }

        private static long SurfaceKey(int x, int y, int z)
        {
            return ((long)x << 40) ^ ((long)y << 20) ^ (uint)z;
        }

        private static bool PreferEdge(GraphEdge next, GraphEdge prev)
        {
            if (next.Cost != prev.Cost) return next.Cost < prev.Cost;
            if (next.RequiresPlacement != prev.RequiresPlacement) return !next.RequiresPlacement;
            if (next.IsConnector != prev.IsConnector) return !next.IsConnector;
            return false;
        }
        private bool TryBuildGeneratedConnector(int minX, int minY, int w, int h, ushort[] baseCost, int[] terrainHeights,
            int lowLx, int lowLy, int highLx, int highLy, out GraphEdge upEdge, out GraphEdge downEdge)
        {
            upEdge = default(GraphEdge);
            downEdge = default(GraphEdge);

            bool InBounds(int x, int y) => x >= 0 && x < w && y >= 0 && y < h;
            int LocalIdx(int x, int y) => y * w + x;
            bool ValidTile(int x, int y, int expectedZ)
            {
                if (!InBounds(x, y)) return false;
                int li = LocalIdx(x, y);
                return baseCost[li] < 255 && terrainHeights[li] == expectedZ;
            }

            int lowIdx = LocalIdx(lowLx, lowLy);
            int highIdx = LocalIdx(highLx, highLy);
            int lowZ = terrainHeights[lowIdx];
            int highZ = terrainHeights[highIdx];
            int levels = highZ - lowZ;
            if (levels <= 0) return false;

            int dx = highLx - lowLx;
            int dy = highLy - lowLy;
            int orientIdx = dx > 0 ? 3 : dx < 0 ? 1 : dy > 0 ? 2 : 0;
            int baseZ = lowZ;
            var rampTiles = new List<(int x, int y, int baseZ, int platCount)>();
            int entLx, entLy, extLx, extLy;

            if (levels == 1)
            {
                int stairLx = lowLx;
                int stairLy = lowLy;
                entLx = stairLx - dx;
                entLy = stairLy - dy;
                extLx = stairLx + dx;
                extLy = stairLy + dy;
                if (!ValidTile(stairLx, stairLy, baseZ) || !ValidTile(entLx, entLy, lowZ) || !ValidTile(extLx, extLy, highZ))
                    return false;
                rampTiles.Add((stairLx + minX, stairLy + minY, baseZ, 0));
            }
            else
            {
                int firstLx = highLx - dx * levels;
                int firstLy = highLy - dy * levels;
                entLx = firstLx - dx;
                entLy = firstLy - dy;
                int lastLx = firstLx + dx * (levels - 1);
                int lastLy = firstLy + dy * (levels - 1);
                extLx = lastLx + dx;
                extLy = lastLy + dy;

                if (!ValidTile(entLx, entLy, lowZ) || !ValidTile(extLx, extLy, highZ))
                    return false;

                for (int step = 0; step < levels; step++)
                {
                    int rampLx = firstLx + dx * step;
                    int rampLy = firstLy + dy * step;
                    if (!ValidTile(rampLx, rampLy, baseZ))
                        return false;
                    rampTiles.Add((rampLx + minX, rampLy + minY, baseZ, step));
                }
            }

            upEdge = new GraphEdge
            {
                Cost = (ushort)(20 * levels),
                IsConnector = true,
                RequiresPlacement = true,
                BaseZ = baseZ,
                Levels = levels,
                EntX = entLx + minX,
                EntY = entLy + minY,
                EntZ = lowZ,
                ExtX = extLx + minX,
                ExtY = extLy + minY,
                ExtZ = highZ,
                OrientIdx = orientIdx,
                RampTiles = rampTiles
            };
            downEdge = new GraphEdge
            {
                Cost = (ushort)(20 * levels),
                IsConnector = true,
                RequiresPlacement = true,
                BaseZ = baseZ,
                Levels = levels,
                EntX = extLx + minX,
                EntY = extLy + minY,
                EntZ = highZ,
                ExtX = entLx + minX,
                ExtY = entLy + minY,
                ExtZ = lowZ,
                OrientIdx = orientIdx,
                RampTiles = rampTiles
            };
            return true;
        }

        // Build a 3D walkable surface graph from terrain data. Each node is a
        // walkable (x,y,z) position. Flat neighbors get cost-weighted edges.
        // Existing stairs become free directed edges. Potential new stair
        // placements become RequiresPlacement edges that A* can use if needed.
        private void BuildSurfaceGraph(PathPlanningData planning,
            out List<SurfaceNode> nodes, out List<GraphEdge>[] adj,
            out Dictionary<long, List<int>> nodesByTile, out int connectorCount)
        {
            int minX = planning.MinX;
            int minY = planning.MinY;
            int w = planning.GridWidth;
            int h = planning.GridHeight;
            var baseCost = new ushort[w * h];
            var terrainHeights = new int[w * h];

            for (int ly = 0; ly < h; ly++)
            {
                for (int lx = 0; lx < w; lx++)
                {
                    int idx = ly * w + lx;
                    int tz = GetTerrainHeight(minX + lx, minY + ly);
                    terrainHeights[idx] = tz;
                    baseCost[idx] = tz > 0 ? (ushort)2 : (ushort)255;
                }
            }

            for (int ly = 0; ly < h; ly++)
            {
                for (int lx = 0; lx < w; lx++)
                {
                    int idx = ly * w + lx;
                    if (baseCost[idx] >= 255) continue;
                    float depth = GetWaterDepth(minX + lx, minY + ly);
                    if (depth > 0.5f) baseCost[idx] = 50;
                    else if (depth > 0f) baseCost[idx] = 8;
                    if (HasOverhang(minX + lx, minY + ly))
                        baseCost[idx] = 255;
                }
            }

            var existingPathSurfaces = new HashSet<long>();
            var existingPlatformSurfaces = new HashSet<long>();
            var existingStairs = new List<(int sx, int sy, int sz, int dir)>();

            for (int i = 0; i < planning.Buildings.Count; i++)
            {
                var cb = planning.Buildings[i];
                bool isPath = cb.Name.Contains("Path");
                bool isStairs = cb.Name.Contains("Stairs");
                bool isPlatform = cb.Name.Contains("Platform");
                bool isReusable = isPath || isStairs || isPlatform;

                if (isStairs)
                    existingStairs.Add((cb.X, cb.Y, cb.Z, ParseOrientation(cb.Orientation ?? "south")));

                for (int t = 0; t < cb.OccupiedTiles.Count; t++)
                {
                    var tile = cb.OccupiedTiles[t];
                    int lx = tile.x - minX;
                    int ly = tile.y - minY;
                    if (lx < 0 || lx >= w || ly < 0 || ly >= h) continue;
                    int idx = ly * w + lx;

                    if (isPath)
                    {
                        existingPathSurfaces.Add(SurfaceKey(tile.x, tile.y, tile.z));
                        if (baseCost[idx] < 255) baseCost[idx] = 1;
                    }
                    else if (isPlatform)
                    {
                        existingPlatformSurfaces.Add(SurfaceKey(tile.x, tile.y, tile.z + 1));
                    }
                    else if (isStairs)
                    {
                        baseCost[idx] = 255;
                    }
                    else if (!isReusable)
                    {
                        baseCost[idx] = 255;
                    }
                }
            }

            for (int i = 0; i < planning.NaturalTiles.Count; i++)
            {
                var nr = planning.NaturalTiles[i];
                int lx = nr.x - minX;
                int ly = nr.y - minY;
                if (lx < 0 || lx >= w || ly < 0 || ly >= h) continue;
                baseCost[ly * w + lx] = 255;
            }

            for (int i = 0; i < planning.BlockerTiles.Count; i++)
            {
                var bt = planning.BlockerTiles[i];
                int lx = bt.x - minX;
                int ly = bt.y - minY;
                if (lx < 0 || lx >= w || ly < 0 || ly >= h) continue;
                baseCost[ly * w + lx] = 255;
            }

            var nodeList = new List<SurfaceNode>();
            var nodeIdBySurface = new Dictionary<long, int>();

            void UpsertNode(int x, int y, int z, ushort entryCost, SurfaceKind kind, bool existing, bool placePath)
            {
                long key = SurfaceKey(x, y, z);
                int id;
                if (nodeIdBySurface.TryGetValue(key, out id))
                {
                    var n = nodeList[id];
                    if (entryCost < n.EntryCost) n.EntryCost = entryCost;
                    if ((byte)kind > (byte)n.Kind) n.Kind = kind;
                    n.Existing = n.Existing || existing;
                    n.PlacePath = n.PlacePath && placePath;
                    nodeList[id] = n;
                    return;
                }

                nodeIdBySurface[key] = nodeList.Count;
                nodeList.Add(new SurfaceNode
                {
                    X = x,
                    Y = y,
                    Z = z,
                    EntryCost = entryCost,
                    Kind = kind,
                    Existing = existing,
                    PlacePath = placePath
                });
            }

            for (int ly = 0; ly < h; ly++)
            {
                for (int lx = 0; lx < w; lx++)
                {
                    int idx = ly * w + lx;
                    if (baseCost[idx] >= 255) continue;
                    int wx = minX + lx;
                    int wy = minY + ly;
                    int wz = terrainHeights[idx];
                    bool hasExistingPath = existingPathSurfaces.Contains(SurfaceKey(wx, wy, wz));
                    UpsertNode(wx, wy, wz, hasExistingPath ? (ushort)1 : baseCost[idx], hasExistingPath ? SurfaceKind.ExistingPath : SurfaceKind.Terrain, hasExistingPath, !hasExistingPath);
                }
            }

            foreach (var s in existingPathSurfaces)
            {
                int x = (int)(s >> 40);
                int y = (int)((s >> 20) & 0xFFFFF);
                int z = (int)(s & 0xFFFFF);
                UpsertNode(x, y, z, 1, SurfaceKind.ExistingPath, true, false);
            }
            foreach (var s in existingPlatformSurfaces)
            {
                int x = (int)(s >> 40);
                int y = (int)((s >> 20) & 0xFFFFF);
                int z = (int)(s & 0xFFFFF);
                UpsertNode(x, y, z, 1, SurfaceKind.ExistingPlatformTop, true, false);
            }

            nodesByTile = new Dictionary<long, List<int>>();
            for (int i = 0; i < nodeList.Count; i++)
            {
                var n = nodeList[i];
                long tileKey = TileKey(n.X, n.Y);
                List<int> list;
                if (!nodesByTile.TryGetValue(tileKey, out list))
                {
                    list = new List<int>();
                    nodesByTile[tileKey] = list;
                }
                list.Add(i);
            }

            var edgeMap = new Dictionary<(int from, int to), GraphEdge>();
            void AddEdge(int fromId, GraphEdge edge)
            {
                var key = (fromId, edge.ToId);
                GraphEdge prev;
                if (!edgeMap.TryGetValue(key, out prev) || PreferEdge(edge, prev))
                    edgeMap[key] = edge;
            }

            int[] ndx = { 1, -1, 0, 0 };
            int[] ndy = { 0, 0, 1, -1 };
            for (int i = 0; i < nodeList.Count; i++)
            {
                var n = nodeList[i];
                for (int d = 0; d < 4; d++)
                {
                    long nk = SurfaceKey(n.X + ndx[d], n.Y + ndy[d], n.Z);
                    int toId;
                    if (!nodeIdBySurface.TryGetValue(nk, out toId)) continue;
                    AddEdge(i, new GraphEdge { ToId = toId, Cost = nodeList[toId].EntryCost, IsConnector = false, RequiresPlacement = false });
                }
            }

            foreach (var s in existingStairs)
            {
                int dx = s.dir == 3 ? 1 : s.dir == 1 ? -1 : 0;
                int dy = s.dir == 2 ? 1 : s.dir == 0 ? -1 : 0;
                int entX = s.sx - dx;
                int entY = s.sy - dy;
                int entZ = s.sz;
                int extX = s.sx + dx;
                int extY = s.sy + dy;
                int extZ = s.sz + 1;

                int fromId, toId;
                if (nodeIdBySurface.TryGetValue(SurfaceKey(entX, entY, entZ), out fromId) && nodeIdBySurface.TryGetValue(SurfaceKey(extX, extY, extZ), out toId))
                {
                    AddEdge(fromId, new GraphEdge
                    {
                        ToId = toId,
                        Cost = 20,
                        IsConnector = true,
                        RequiresPlacement = false,
                        BaseZ = s.sz,
                        Levels = 1,
                        EntX = entX,
                        EntY = entY,
                        EntZ = entZ,
                        ExtX = extX,
                        ExtY = extY,
                        ExtZ = extZ,
                        OrientIdx = s.dir
                    });
                }
                if (nodeIdBySurface.TryGetValue(SurfaceKey(extX, extY, extZ), out fromId) && nodeIdBySurface.TryGetValue(SurfaceKey(entX, entY, entZ), out toId))
                {
                    AddEdge(fromId, new GraphEdge
                    {
                        ToId = toId,
                        Cost = 20,
                        IsConnector = true,
                        RequiresPlacement = false,
                        BaseZ = s.sz,
                        Levels = 1,
                        EntX = extX,
                        EntY = extY,
                        EntZ = extZ,
                        ExtX = entX,
                        ExtY = entY,
                        ExtZ = entZ,
                        OrientIdx = s.dir
                    });
                }
            }

            for (int ly = 0; ly < h; ly++)
            {
                for (int lx = 0; lx < w; lx++)
                {
                    int idx = ly * w + lx;
                    if (baseCost[idx] >= 255) continue;

                    if (lx + 1 < w && baseCost[idx + 1] < 255)
                    {
                        int z0 = terrainHeights[idx];
                        int z1 = terrainHeights[idx + 1];
                        if (z0 != z1)
                        {
                            int lowLx = z0 < z1 ? lx : lx + 1;
                            int lowLy = ly;
                            int highLx = z0 < z1 ? lx + 1 : lx;
                            int highLy = ly;
                            GraphEdge upEdge, downEdge;
                            if (TryBuildGeneratedConnector(minX, minY, w, h, baseCost, terrainHeights, lowLx, lowLy, highLx, highLy, out upEdge, out downEdge))
                            {
                                int fromId, toId;
                                if (nodeIdBySurface.TryGetValue(SurfaceKey(upEdge.EntX, upEdge.EntY, upEdge.EntZ), out fromId) && nodeIdBySurface.TryGetValue(SurfaceKey(upEdge.ExtX, upEdge.ExtY, upEdge.ExtZ), out toId))
                                {
                                    upEdge.ToId = toId;
                                    AddEdge(fromId, upEdge);
                                }
                                if (nodeIdBySurface.TryGetValue(SurfaceKey(downEdge.EntX, downEdge.EntY, downEdge.EntZ), out fromId) && nodeIdBySurface.TryGetValue(SurfaceKey(downEdge.ExtX, downEdge.ExtY, downEdge.ExtZ), out toId))
                                {
                                    downEdge.ToId = toId;
                                    AddEdge(fromId, downEdge);
                                }
                            }
                        }
                    }

                    if (ly + 1 < h && baseCost[idx + w] < 255)
                    {
                        int z0 = terrainHeights[idx];
                        int z1 = terrainHeights[idx + w];
                        if (z0 != z1)
                        {
                            int lowLx = lx;
                            int lowLy = z0 < z1 ? ly : ly + 1;
                            int highLx = lx;
                            int highLy = z0 < z1 ? ly + 1 : ly;
                            GraphEdge upEdge, downEdge;
                            if (TryBuildGeneratedConnector(minX, minY, w, h, baseCost, terrainHeights, lowLx, lowLy, highLx, highLy, out upEdge, out downEdge))
                            {
                                int fromId, toId;
                                if (nodeIdBySurface.TryGetValue(SurfaceKey(upEdge.EntX, upEdge.EntY, upEdge.EntZ), out fromId) && nodeIdBySurface.TryGetValue(SurfaceKey(upEdge.ExtX, upEdge.ExtY, upEdge.ExtZ), out toId))
                                {
                                    upEdge.ToId = toId;
                                    AddEdge(fromId, upEdge);
                                }
                                if (nodeIdBySurface.TryGetValue(SurfaceKey(downEdge.EntX, downEdge.EntY, downEdge.EntZ), out fromId) && nodeIdBySurface.TryGetValue(SurfaceKey(downEdge.ExtX, downEdge.ExtY, downEdge.ExtZ), out toId))
                                {
                                    downEdge.ToId = toId;
                                    AddEdge(fromId, downEdge);
                                }
                            }
                        }
                    }
                }
            }

            nodes = nodeList;
            adj = new List<GraphEdge>[nodeList.Count];
            for (int i = 0; i < adj.Length; i++) adj[i] = new List<GraphEdge>();
            connectorCount = 0;
            foreach (var kv in edgeMap)
            {
                adj[kv.Key.from].Add(kv.Value);
                if (kv.Value.IsConnector) connectorCount++;
            }
        }

        // Safe A* over the coherent surface graph. Plain Manhattan on x/y remains admissible because
        // every traversable edge has minimum cost >= 1. style only acts as a secondary tie-break.
        private static SurfacePath AStarPath(List<SurfaceNode> nodes, List<GraphEdge>[] adj, List<int> startIds, List<int> goalIds, int maxNodes, string style)
        {
            if (startIds == null || startIds.Count == 0 || goalIds == null || goalIds.Count == 0) return null;

            bool straight = style == "straight";
            var goalSet = new HashSet<int>(goalIds);
            int Heuristic(int nodeId)
            {
                var n = nodes[nodeId];
                int best = int.MaxValue;
                for (int i = 0; i < goalIds.Count; i++)
                {
                    var g = nodes[goalIds[i]];
                    int h = System.Math.Abs(g.X - n.X) + System.Math.Abs(g.Y - n.Y);
                    if (h < best) best = h;
                }
                return best;
            }

            var gScore = new Dictionary<int, int>();
            var openScore = new Dictionary<int, (int f, int bias)>();
            var cameFrom = new Dictionary<int, int>();
            var cameEdge = new Dictionary<int, GraphEdge>();
            var open = new SortedSet<(int f, int bias, int idx)>();

            for (int i = 0; i < startIds.Count; i++)
            {
                int sid = startIds[i];
                gScore[sid] = 0;
                int h0 = Heuristic(sid);
                openScore[sid] = (h0, 0);
                open.Add((h0, 0, sid));
            }

            int nodesExpanded = 0;
            while (open.Count > 0)
            {
                var current = open.Min;
                open.Remove(current);
                int cidx = current.idx;

                if (goalSet.Contains(cidx))
                {
                    var nodePath = new List<int>();
                    var edgePath = new List<GraphEdge>();
                    int cur = cidx;
                    nodePath.Add(cur);
                    while (cameFrom.ContainsKey(cur))
                    {
                        edgePath.Add(cameEdge[cur]);
                        cur = cameFrom[cur];
                        nodePath.Add(cur);
                    }
                    nodePath.Reverse();
                    edgePath.Reverse();
                    return new SurfacePath { Nodes = nodePath, Edges = edgePath };
                }

                if (++nodesExpanded > maxNodes) return null;
                int cg = gScore[cidx];

                int prevDx = 0, prevDy = 0;
                if (straight && cameFrom.ContainsKey(cidx))
                {
                    var prevNode = nodes[cameFrom[cidx]];
                    var curNode = nodes[cidx];
                    prevDx = System.Math.Sign(curNode.X - prevNode.X);
                    prevDy = System.Math.Sign(curNode.Y - prevNode.Y);
                }

                foreach (var edge in adj[cidx])
                {
                    int nidx = edge.ToId;
                    int tentG = cg + edge.Cost;
                    int prevG;
                    if (gScore.TryGetValue(nidx, out prevG) && tentG >= prevG) continue;

                    cameFrom[nidx] = cidx;
                    cameEdge[nidx] = edge;
                    gScore[nidx] = tentG;

                    var curNode = nodes[cidx];
                    var nextNode = nodes[nidx];
                    int moveDx = System.Math.Sign(nextNode.X - curNode.X);
                    int moveDy = System.Math.Sign(nextNode.Y - curNode.Y);
                    int bias;
                    if (straight)
                    {
                        bool sameDir = (moveDx == prevDx && moveDy == prevDy) || (prevDx == 0 && prevDy == 0);
                        bias = sameDir ? 0 : 2;
                    }
                    else
                    {
                        var target = nodes[goalIds[0]];
                        int remX = System.Math.Abs(target.X - nextNode.X);
                        int remY = System.Math.Abs(target.Y - nextNode.Y);
                        int remXbefore = System.Math.Abs(target.X - curNode.X);
                        int remYbefore = System.Math.Abs(target.Y - curNode.Y);
                        bool reducedX = remX < remXbefore;
                        bool reducedY = remY < remYbefore;
                        if (reducedX && remXbefore >= remYbefore) bias = 0;
                        else if (reducedY && remYbefore >= remXbefore) bias = 0;
                        else if (reducedX || reducedY) bias = 1;
                        else bias = 3;
                    }

                    int nf = tentG + Heuristic(nidx);
                    (int f, int bias) prevOpen;
                    if (openScore.TryGetValue(nidx, out prevOpen))
                        open.Remove((prevOpen.f, prevOpen.bias, nidx));
                    openScore[nidx] = (nf, bias);
                    open.Add((nf, bias, nidx));
                }
            }

            return null;
        }
        // general purpose debug endpoint. navigate, inspect, and call methods on any game object
        // chain through objects with dot-separated paths: "type._field1._field2.MethodName"
        // ================================================================
        // BENCHMARK. compare foreach vs for-loop on game collections
        // ================================================================


        // Find valid building placement spots in a rectangular search area.
        // This is the most complex method in the mod. It does 5 things:
        //
        // 1. REACHABILITY: uses reflection to access the game's internal NavMesh
        //    and determine which tiles are connected to the district center via paths.
        //    This is the same data the game uses to draw green/red path indicators.
        //
        // 2. PATH SCORING: counts how many path tiles are adjacent to each candidate's
        //    entrance side. More paths = better connectivity.
        //
        // 3. POWER ADJACENCY: checks if any power-conducting building is adjacent to
        //    the placement footprint. Buildings need to be adjacent to conduct power.
        //
        // 4. FLOOD CHECK: checks WaterDepth > 0 on ground-required tiles only
        //    (MatterBelow.GroundOrStackable). Water intake tiles are expected wet.
        //
        private sealed class FindPlacementJob : ITimberbotWriteJob
        {
            private readonly TimberbotPlacement _owner;
            private readonly string _prefabName;
            private readonly string _format;
            private readonly int _x1;
            private readonly int _y1;
            private readonly int _x2;
            private readonly int _y2;
            private readonly string[] _orientNames = { "south", "west", "north", "east" };
            private readonly List<(int x, int y, int z, int orient, bool pathAccess, bool reachable, float distance, bool nearPower, bool flooded, float waterDepth, int entranceX, int entranceY)> _results
                = new List<(int x, int y, int z, int orient, bool pathAccess, bool reachable, float distance, bool nearPower, bool flooded, float waterDepth, int entranceX, int entranceY)>();
            private Dictionary<Vector3Int, float> _reachableRoadCoords;
            private HashSet<long> _pathTiles;
            private HashSet<long> _powerTiles;
            private Preview _cachedPreview;
            private BuildingSpec _buildingSpec;
            private Vector3Int _size;
            private Vector3Int? _waterInputLocal;
            private int _tx;
            private int _ty;
            private int _minX;
            private int _maxX;
            private int _minY;
            private int _maxY;
            private bool _initialized;
            private bool _completed;
            private int _statusCode = 200;
            private object _result;

            public FindPlacementJob(TimberbotPlacement owner, string prefabName, int x1, int y1, int x2, int y2, string format)
            {
                _owner = owner;
                _prefabName = prefabName;
                _format = format ?? "toon";
                _x1 = x1;
                _y1 = y1;
                _x2 = x2;
                _y2 = y2;
            }

            public string Name => "/api/placement/find";
            public bool IsCompleted => _completed;
            public int StatusCode => _statusCode;
            public object Result => _result;

            public void Step(float now, double budgetMs)
            {
                if (_completed) return;
                if (!_initialized && !Initialize(now)) return;

                var sw = System.Diagnostics.Stopwatch.StartNew();
                while (_ty <= _maxY)
                {
                    EvaluateTile(_tx, _ty);
                    _tx++;
                    if (_tx > _maxX)
                    {
                        _tx = _minX;
                        _ty++;
                    }

                    if (sw.Elapsed.TotalMilliseconds >= budgetMs)
                        return;
                }

                FinalizeResult();
            }

            public void Cancel(string error)
            {
                if (_completed) return;
                CleanupPreview();
                _statusCode = 500;
                _result = "{\"error\":\"" + error.Replace("\"", "'") + "\"}";
                _completed = true;
            }

            private bool Initialize(float now)
            {
                _initialized = true;
                try { _buildingSpec = _owner._buildingService.GetBuildingTemplate(_prefabName); }
                catch
                {
                    _result = _owner.Jw.Error(_owner.InvalidPrefabError(_prefabName), ("prefab", _prefabName));
                    _completed = true;
                    return false;
                }
                if (_buildingSpec == null)
                {
                    _result = _owner.Jw.Error(_owner.InvalidPrefabError(_prefabName), ("prefab", _prefabName));
                    _completed = true;
                    return false;
                }

                var blockObjectSpec = _buildingSpec.GetSpec<BlockObjectSpec>();
                if (blockObjectSpec == null)
                {
                    _result = _owner.Jw.Error("invalid_type: no block object spec", ("prefab", _prefabName));
                    _completed = true;
                    return false;
                }

                _size = blockObjectSpec.Size;
                var waterInputSpec = _buildingSpec.GetSpec<WaterInputSpec>();
                _waterInputLocal = waterInputSpec != null ? (Vector3Int?)waterInputSpec.WaterInputCoordinates : null;
                _minX = System.Math.Min(_x1, _x2);
                _maxX = System.Math.Max(_x1, _x2);
                _minY = System.Math.Min(_y1, _y2);
                _maxY = System.Math.Max(_y1, _y2);

                if (_minX == 0 && _maxX == 0 && _minY == 0 && _maxY == 0)
                {
                    var mapSize = _owner._terrainService.Size;
                    int cx = mapSize.x / 2, cy = mapSize.y / 2, r = TimberbotService.DefaultSearchRadius;
                    foreach (var dc in _owner._districtCenterRegistry.FinishedDistrictCenters)
                    {
                        var bo = dc.GetComponent<BlockObject>();
                        if (bo != null) { cx = bo.Coordinates.x; cy = bo.Coordinates.y; break; }
                    }
                    _minX = System.Math.Max(0, cx - r);
                    _maxX = System.Math.Min(mapSize.x - 1, cx + r);
                    _minY = System.Math.Max(0, cy - r);
                    _maxY = System.Math.Min(mapSize.y - 1, cy + r);
                }
                _tx = _minX;
                _ty = _minY;

                _reachableRoadCoords = new Dictionary<Vector3Int, float>();
                try
                {
                    var reflFlags = System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance;
                    var nodeIdSvc = _owner._navMeshService.GetType().GetField("_nodeIdService", reflFlags)
                        ?.GetValue(_owner._navMeshService) as Timberborn.Navigation.NodeIdService;

                    foreach (var dc in _owner._districtCenterRegistry.FinishedDistrictCenters)
                    {
                        var cachingFF = dc.GetComponent<BuildingCachingFlowField>();
                        if (cachingFF == null || nodeIdSvc == null) continue;
                        var accessCoords = (Vector3Int)cachingFF.GetType().GetField("_accessCoordinates", reflFlags).GetValue(cachingFF);
                        int dcNodeId = nodeIdSvc.GridToId(accessCoords);
                        Vector3 dcWorldPos = nodeIdSvc.IdToWorld(dcNodeId);

                        var drawer = dc.GetComponent<DistrictPathNavRangeDrawer>();
                        if (drawer == null) continue;
                        var navRangeSvc = drawer.GetType().GetField("_navigationRangeService", reflFlags)?.GetValue(drawer);
                        if (navRangeSvc == null) continue;

                        var nodesInRange = navRangeSvc.GetType().GetMethod("GetRoadNodesInRange")
                            ?.Invoke(navRangeSvc, new object[] { dcWorldPos }) as System.Collections.IEnumerable;
                        if (nodesInRange == null) continue;

                        foreach (var wc in nodesInRange)
                        {
                            var wcType = wc.GetType();
                            var coordsProp = wcType.GetProperty("Coordinates");
                            var distProp = wcType.GetProperty("Distance");
                            if (coordsProp != null)
                            {
                                var coords = (Vector3Int)coordsProp.GetValue(wc);
                                float dist = distProp != null ? (float)distProp.GetValue(wc) : -1f;
                                _reachableRoadCoords[coords] = dist;
                            }
                        }
                        break;
                    }
                }
                catch (System.Exception ex)
                {
                    TimberbotLog.Error("placement", ex);
                }

                _pathTiles = new HashSet<long>();
                _powerTiles = new HashSet<long>();
                var buildingSnapshot = _owner._readV2.EnsureBuildingsFreshNow(now);
                for (int i = 0; i < buildingSnapshot.Count; i++)
                {
                    var cb = buildingSnapshot.Definitions[i];
                    if (cb.Name.Contains("Path") || cb.Name.Contains("Stairs"))
                    {
                        foreach (var c in cb.OccupiedTiles)
                            _pathTiles.Add((long)c.x * 1000000 + (long)c.y * 1000 + c.z);
                    }
                    if (cb.HasPowerNode != 0)
                    {
                        foreach (var c in cb.OccupiedTiles)
                            _powerTiles.Add((long)c.x * 1000000 + (long)c.y * 1000 + c.z);
                    }
                }

                var placeableSpec = _buildingSpec.GetSpec<PlaceableBlockObjectSpec>();
                try
                {
                    if (placeableSpec != null)
                        _cachedPreview = _owner._previewFactory.Create(placeableSpec);
                }
                catch (System.Exception ex)
                {
                    TimberbotLog.Error("placement", ex);
                }

                return true;
            }

            private void EvaluateTile(int tx, int ty)
            {
                int tz = _owner.GetTerrainHeight(tx, ty);
                if (tz <= 0 || _cachedPreview == null) return;

                int bestOrient = -1;
                bool bestHasPath = false;

                for (int orient = 0; orient < 4; orient++)
                {
                    int vrx = _size.x;
                    int vry = _size.y;
                    if (orient == 1 || orient == 3) { vrx = _size.y; vry = _size.x; }
                    int vgx = tx;
                    int vgy = ty;
                    switch (orient)
                    {
                        case 1: vgy = ty + vry - 1; break;
                        case 2: vgx = tx + vrx - 1; vgy = ty + vry - 1; break;
                        case 3: vgx = tx + vrx - 1; break;
                    }

                    var placement = new Placement(new Vector3Int(vgx, vgy, tz), (Timberborn.Coordinates.Orientation)orient, FlipMode.Unflipped);
                    _cachedPreview.Reposition(placement);
                    if (!_cachedPreview.BlockObject.IsValid()) continue;

                    bool hasPath = false;
                    if (_cachedPreview.BlockObject.HasEntrance)
                    {
                        var ds = _cachedPreview.BlockObject.PositionedEntrance.Coordinates;
                        hasPath = _pathTiles.Contains((long)ds.x * 1000000 + (long)ds.y * 1000 + ds.z);
                    }

                    if ((hasPath && !bestHasPath) || (!bestHasPath && bestOrient < 0))
                    {
                        bestHasPath = hasPath;
                        bestOrient = orient;
                    }
                }

                if (bestOrient < 0) return;

                int brx2 = _size.x;
                int bry2 = _size.y;
                if (bestOrient == 1 || bestOrient == 3) { brx2 = _size.y; bry2 = _size.x; }
                int bgx = tx;
                int bgy = ty;
                switch (bestOrient)
                {
                    case 1: bgy = ty + bry2 - 1; break;
                    case 2: bgx = tx + brx2 - 1; bgy = ty + bry2 - 1; break;
                    case 3: bgx = tx + brx2 - 1; break;
                }
                _cachedPreview.Reposition(new Placement(new Vector3Int(bgx, bgy, tz), (Timberborn.Coordinates.Orientation)bestOrient, FlipMode.Unflipped));

                // use the preview's actual base z (handles water-edge pumps that span z levels)
                int actualZ = _cachedPreview.BlockObject.CoordinatesAtBaseZ.z;

                int entranceX = tx;
                int entranceY = ty;
                if (_cachedPreview.BlockObject.HasEntrance)
                {
                    var ds = _cachedPreview.BlockObject.PositionedEntrance.Coordinates;
                    entranceX = ds.x;
                    entranceY = ds.y;
                }

                bool reachable = false;
                float distance = -1f;
                if (bestHasPath && _cachedPreview.BlockObject.HasEntrance)
                {
                    var ds = _cachedPreview.BlockObject.PositionedEntrance.Coordinates;
                    if (_reachableRoadCoords.TryGetValue(ds, out float dist))
                    {
                        reachable = true;
                        distance = dist;
                    }
                }

                bool nearPower = false;
                for (int px = tx - 1; px <= tx + brx2 && !nearPower; px++)
                    for (int py = ty - 1; py <= ty + bry2 && !nearPower; py++)
                    {
                        if (px >= tx && px < tx + brx2 && py >= ty && py < ty + bry2) continue;
                        if (_powerTiles.Contains((long)px * 1000000 + (long)py * 1000 + tz))
                            nearPower = true;
                    }

                bool flooded = false;
                float waterDepth = 0f;
                foreach (var block in _cachedPreview.BlockObject.PositionedBlocks.GetAllBlocks())
                {
                    var c = block.Coordinates;
                    if (c.z != tz) continue;
                    float depth = _owner.GetWaterDepth(c.x, c.y);
                    if (block.MatterBelow == MatterBelow.GroundOrStackable)
                    {
                        if (depth > 0) flooded = true;
                    }
                    else if (block.MatterBelow != MatterBelow.Air && _waterInputLocal.HasValue)
                    {
                        if (depth > waterDepth) waterDepth = depth;
                    }
                }

                _results.Add((tx, ty, actualZ, bestOrient, bestHasPath, reachable, distance, nearPower, flooded, waterDepth, entranceX, entranceY));
            }

            private void FinalizeResult()
            {
                _results.Sort((a, b) =>
                {
                    if (_waterInputLocal.HasValue && a.waterDepth != b.waterDepth) return b.waterDepth.CompareTo(a.waterDepth);
                    if (a.flooded != b.flooded) return a.flooded ? 1 : -1;
                    if (a.reachable != b.reachable) return b.reachable ? 1 : -1;
                    if (a.distance != b.distance)
                    {
                        if (a.distance < 0) return 1;
                        if (b.distance < 0) return -1;
                        return a.distance.CompareTo(b.distance);
                    }
                    if (a.pathAccess != b.pathAccess) return b.pathAccess ? 1 : -1;
                    if (a.nearPower != b.nearPower) return b.nearPower ? 1 : -1;
                    return 0;
                });

                int count = _results.Count > 10 ? 10 : _results.Count;
                var jw = _owner.Jw.Reset();
                if (_format == "toon")
                {
                    // toon: flat array (same keys as json, toons library renders as compact table)
                    jw.OpenArr();
                    for (int i = 0; i < count; i++)
                    {
                        var r = _results[i];
                        jw.OpenObj()
                            .Prop("x", r.x).Prop("y", r.y).Prop("z", r.z)
                            .Prop("orientation", _orientNames[r.orient])
                            .Prop("entranceX", r.entranceX).Prop("entranceY", r.entranceY)
                            .Prop("pathAccess", r.pathAccess ? 1 : 0)
                            .Prop("reachable", r.reachable ? 1 : 0)
                            .Prop("distance", r.distance, "F1")
                            .Prop("nearPower", r.nearPower ? 1 : 0)
                            .Prop("flooded", r.flooded ? 1 : 0);
                        if (_waterInputLocal.HasValue)
                            jw.Prop("waterDepth", r.waterDepth, "F2");
                        jw.CloseObj();
                    }
                    _result = jw.CloseArr().ToString();
                }
                else
                {
                    // json: nested object with metadata
                    jw.BeginObj()
                        .Prop("prefab", _prefabName)
                        .Prop("sizeX", _size.x).Prop("sizeY", _size.y)
                        .Arr("placements");
                    for (int i = 0; i < count; i++)
                    {
                        var r = _results[i];
                        jw.OpenObj()
                            .Prop("x", r.x).Prop("y", r.y).Prop("z", r.z)
                            .Prop("orientation", _orientNames[r.orient])
                            .Prop("entranceX", r.entranceX).Prop("entranceY", r.entranceY)
                            .Prop("pathAccess", r.pathAccess ? 1 : 0)
                            .Prop("reachable", r.reachable ? 1 : 0)
                            .Prop("distance", r.distance, "F1")
                            .Prop("nearPower", r.nearPower ? 1 : 0)
                            .Prop("flooded", r.flooded ? 1 : 0);
                        if (_waterInputLocal.HasValue)
                            jw.Prop("waterDepth", r.waterDepth, "F2");
                        jw.CloseObj();
                    }
                    _result = jw.CloseArr().CloseObj().ToString();
                }
                CleanupPreview();
                _completed = true;
            }

            private void CleanupPreview()
            {
                if (_cachedPreview != null)
                {
                    UnityEngine.Object.Destroy(_cachedPreview.GameObject);
                    _cachedPreview = null;
                }
            }
        }

        // 5. PLACEMENT VALIDATION: uses the game's own PreviewFactory to create a
        //    preview entity, Reposition it, and check IsValid(). This runs the same
        //    9 validators the player UI uses (terrain, occupancy, water buildings, etc).
        //
        // Results sorted by: non-flooded > reachable > distance (closer) > pathAccess > nearPower.
        // Returns top 10 candidates.
        public object FindPlacement(string prefabName, int x1, int y1, int x2, int y2)
        {
            BuildingSpec buildingSpec;
            try { buildingSpec = _buildingService.GetBuildingTemplate(prefabName); }
            catch { return Jw.Error(InvalidPrefabError(prefabName), ("prefab", prefabName)); }
            if (buildingSpec == null)
                return Jw.Error(InvalidPrefabError(prefabName), ("prefab", prefabName));
            var blockObjectSpec = buildingSpec.GetSpec<BlockObjectSpec>();
            if (blockObjectSpec == null)
                return Jw.Error("invalid_type: no block object spec", ("prefab", prefabName));

            var size = blockObjectSpec.Size;
            var waterInputSpec = buildingSpec.GetSpec<WaterInputSpec>();
            Vector3Int? waterInputLocal = waterInputSpec != null
                ? (Vector3Int?)waterInputSpec.WaterInputCoordinates : null;

            // STEP 1: REACHABILITY
            // Use reflection to access the game's NavMesh internals. These APIs are
            // private because they're not meant for mods, but we need them to determine
            // if a building site is connected to the district center via paths.
            var reachableRoadCoords = new Dictionary<Vector3Int, float>();
            try
            {
                var reflFlags = System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance;
                var nodeIdSvc = _navMeshService.GetType().GetField("_nodeIdService", reflFlags)
                    ?.GetValue(_navMeshService) as Timberborn.Navigation.NodeIdService;

                foreach (var dc in _districtCenterRegistry.FinishedDistrictCenters)
                {
                    var cachingFF = dc.GetComponent<BuildingCachingFlowField>();
                    if (cachingFF == null || nodeIdSvc == null) continue;
                    var accessCoords = (Vector3Int)cachingFF.GetType().GetField("_accessCoordinates", reflFlags).GetValue(cachingFF);
                    int dcNodeId = nodeIdSvc.GridToId(accessCoords);
                    Vector3 dcWorldPos = nodeIdSvc.IdToWorld(dcNodeId);

                    var drawer = dc.GetComponent<DistrictPathNavRangeDrawer>();
                    if (drawer == null) continue;
                    var navRangeSvc = drawer.GetType().GetField("_navigationRangeService", reflFlags)?.GetValue(drawer);
                    if (navRangeSvc == null) continue;

                    var nodesInRange = navRangeSvc.GetType().GetMethod("GetRoadNodesInRange")
                        ?.Invoke(navRangeSvc, new object[] { dcWorldPos }) as System.Collections.IEnumerable;
                    if (nodesInRange == null) continue;

                    foreach (var wc in nodesInRange)
                    {
                        var wcType = wc.GetType();
                        var coordsProp = wcType.GetProperty("Coordinates");
                        var distProp = wcType.GetProperty("Distance");
                        if (coordsProp != null)
                        {
                            var coords = (Vector3Int)coordsProp.GetValue(wc);
                            float dist = distProp != null ? (float)distProp.GetValue(wc) : -1f;
                            reachableRoadCoords[coords] = dist;
                        }
                    }
                    break;
                }
            }
            catch (System.Exception _ex) { TimberbotLog.Error("placement", _ex); }

            // Build HashSets of path and power tile positions for O(1) adjacency checks.
            // Tiles are encoded as a single long: x*1000000 + y*1000 + z
            // This avoids allocating Vector3Int keys and works for maps up to 999x999x999.
            var pathTiles = new HashSet<long>();
            var powerTiles = new HashSet<long>();
            var buildingSnapshot = _readV2.EnsureBuildingsFreshNow(Time.realtimeSinceStartup);
            for (int i = 0; i < buildingSnapshot.Count; i++)
            {
                var cb = buildingSnapshot.Definitions[i];
                // paths and stairs provide connectivity for reachability scoring
                if (cb.Name.Contains("Path") || cb.Name.Contains("Stairs"))
                {
                    foreach (var c in cb.OccupiedTiles)
                        pathTiles.Add((long)c.x * 1000000 + (long)c.y * 1000 + c.z);
                }
                // power-conducting buildings (anything with a PowerNode component)
                if (cb.HasPowerNode != 0)
                {
                    foreach (var c in cb.OccupiedTiles)
                        powerTiles.Add((long)c.x * 1000000 + (long)c.y * 1000 + c.z);
                }
            }

            var orientNames = new[] { "south", "west", "north", "east" };
            var results = new List<(int x, int y, int z, int orient, bool pathAccess, bool reachable, float distance, bool nearPower, bool flooded, float waterDepth, int entranceX, int entranceY)>();

            // PERF: create ONE preview entity, reuse it for every candidate position.
            // Preview is a Unity GameObject with validation components attached.
            // Creating one per candidate would be ~1000x slower (Instantiate + Destroy).
            // Reposition() moves the same preview to each new position for validation.
            var placeableSpec = buildingSpec.GetSpec<PlaceableBlockObjectSpec>();
            Preview cachedPreview = null;
            try { if (placeableSpec != null) cachedPreview = _previewFactory.Create(placeableSpec); } catch (System.Exception _ex) { TimberbotLog.Error("placement", _ex); }
            try
            {

                for (int ty = y1; ty <= y2; ty++)
                {
                    for (int tx = x1; tx <= x2; tx++)
                    {
                        int tz = GetTerrainHeight(tx, ty);
                        if (tz <= 0) continue;

                        // Try all 4 orientations and pick the one with the most adjacent
                        // path tiles on its entrance side. This maximizes connectivity.
                        int bestOrient = -1;
                        bool bestHasPath = false;

                        for (int orient = 0; orient < 4; orient++)
                        {
                            // validate using the cached preview (game's own placement rules)
                            if (cachedPreview == null) continue;
                            // orient 1,3 (west/east) swap x and y dimensions of the footprint
                            int vrx = size.x, vry = size.y;
                            if (orient == 1 || orient == 3) { vrx = size.y; vry = size.x; }
                            // origin correction: the game uses top-right corner as origin for some
                            // orientations. We always think in bottom-left, so translate.
                            int vgx = tx, vgy = ty;
                            switch (orient)
                            {
                                case 1: vgy = ty + vry - 1; break;
                                case 2: vgx = tx + vrx - 1; vgy = ty + vry - 1; break;
                                case 3: vgx = tx + vrx - 1; break;
                            }
                            var placement = new Placement(new Vector3Int(vgx, vgy, tz),
                                (Timberborn.Coordinates.Orientation)orient, FlipMode.Unflipped);
                            cachedPreview.Reposition(placement);
                            if (!cachedPreview.BlockObject.IsValid()) continue;

                            // check if doorstep tile (in front of entrance) has a path
                            bool hasPath = false;
                            if (cachedPreview.BlockObject.HasEntrance)
                            {
                                var ds = cachedPreview.BlockObject.PositionedEntrance.Coordinates;
                                hasPath = pathTiles.Contains((long)ds.x * 1000000 + (long)ds.y * 1000 + ds.z);
                            }

                            // prefer orientation with path access, then first valid
                            if (hasPath && !bestHasPath || (!bestHasPath && bestOrient < 0))
                            {
                                bestHasPath = hasPath;
                                bestOrient = orient;
                            }
                        }

                        if (bestOrient >= 0)
                        {
                            // reposition preview to best orientation to read game state
                            int brx2 = size.x, bry2 = size.y;
                            if (bestOrient == 1 || bestOrient == 3) { brx2 = size.y; bry2 = size.x; }
                            int bgx = tx, bgy = ty;
                            switch (bestOrient)
                            {
                                case 1: bgy = ty + bry2 - 1; break;
                                case 2: bgx = tx + brx2 - 1; bgy = ty + bry2 - 1; break;
                                case 3: bgx = tx + brx2 - 1; break;
                            }
                            cachedPreview.Reposition(new Placement(new Vector3Int(bgx, bgy, tz),
                                (Timberborn.Coordinates.Orientation)bestOrient, FlipMode.Unflipped));

                            // read doorstep coords
                            int entranceX = tx, entranceY = ty;
                            if (cachedPreview.BlockObject.HasEntrance)
                            {
                                var ds = cachedPreview.BlockObject.PositionedEntrance.Coordinates;
                                entranceX = ds.x;
                                entranceY = ds.y;
                            }

                            // check reachability: doorstep tile in reachable road network
                            bool reachable = false;
                            float distance = -1f;
                            if (bestHasPath && cachedPreview.BlockObject.HasEntrance)
                            {
                                var ds = cachedPreview.BlockObject.PositionedEntrance.Coordinates;
                                if (reachableRoadCoords.TryGetValue(ds, out float dist))
                                {
                                    reachable = true;
                                    distance = dist;
                                }
                            }

                            // check power adjacency on all 4 sides of footprint
                            bool nearPower = false;
                            for (int px = tx - 1; px <= tx + brx2 && !nearPower; px++)
                                for (int py = ty - 1; py <= ty + bry2 && !nearPower; py++)
                                {
                                    if (px >= tx && px < tx + brx2 && py >= ty && py < ty + bry2) continue;
                                    if (powerTiles.Contains((long)px * 1000000 + (long)py * 1000 + tz))
                                        nearPower = true;
                                }

                            // use the game's positioned blocks (already rotated) for flooding + water depth
                            bool flooded = false;
                            float waterDepth = 0f;
                            foreach (var block in cachedPreview.BlockObject.PositionedBlocks.GetAllBlocks())
                            {
                                var c = block.Coordinates;
                                if (c.z != tz) continue;
                                float depth = GetWaterDepth(c.x, c.y);
                                if (block.MatterBelow == MatterBelow.GroundOrStackable)
                                {
                                    if (depth > 0) flooded = true;
                                }
                                else if (block.MatterBelow != MatterBelow.Air && waterInputLocal.HasValue)
                                {
                                    if (depth > waterDepth) waterDepth = depth;
                                }
                            }

                            results.Add((tx, ty, tz, bestOrient, bestHasPath, reachable, distance, nearPower, flooded, waterDepth, entranceX, entranceY));
                        }
                    }
                }

                // sort: water buildings prioritize waterDepth first, others prioritize non-flooded
                results.Sort((a, b) =>
                {
                    if (waterInputLocal.HasValue)
                    {
                        if (a.waterDepth != b.waterDepth) return b.waterDepth.CompareTo(a.waterDepth);
                    }
                    if (a.flooded != b.flooded) return a.flooded ? 1 : -1;
                    if (a.reachable != b.reachable) return b.reachable ? 1 : -1;
                    if (a.distance != b.distance)
                    {
                        // both unreachable (-1) are equal; otherwise closer to DC wins
                        if (a.distance < 0) return 1;
                        if (b.distance < 0) return -1;
                        return a.distance.CompareTo(b.distance);
                    }
                    if (a.pathAccess != b.pathAccess) return b.pathAccess ? 1 : -1;
                    if (a.nearPower != b.nearPower) return b.nearPower ? 1 : -1;
                    return 0;
                });

            } // end try
            finally
            {
                if (cachedPreview != null)
                    UnityEngine.Object.Destroy(cachedPreview.GameObject);
            }

            int count = results.Count > 10 ? 10 : results.Count;

            var jw = Jw.Reset().BeginObj()
                .Prop("prefab", prefabName)
                .Prop("sizeX", size.x).Prop("sizeY", size.y)
                .Arr("placements");
            for (int i = 0; i < count; i++)
            {
                var r = results[i];
                jw.OpenObj()
                    .Prop("x", r.x).Prop("y", r.y).Prop("z", r.z)
                    .Prop("orientation", orientNames[r.orient])
                    .Prop("entranceX", r.entranceX).Prop("entranceY", r.entranceY)
                    .Prop("pathAccess", r.pathAccess ? 1 : 0)
                    .Prop("reachable", r.reachable ? 1 : 0)
                    .Prop("distance", r.distance, "F1")
                    .Prop("nearPower", r.nearPower ? 1 : 0)
                    .Prop("flooded", r.flooded ? 1 : 0);
                if (waterInputLocal.HasValue)
                    jw.Prop("waterDepth", r.waterDepth, "F2");
                jw.CloseObj();
            }
            jw.CloseArr().CloseObj();
            return jw.ToString();
        }

        // Place a building at exact coordinates with full validation:
        // 1. Prefab must exist in BuildingService and be unlocked (if it costs science)
        // 2. Origin correction: the user specifies bottom-left corner, but the game's
        //    internal coordinate system uses a different origin per orientation.
        //    We translate so the caller never has to think about rotation math.
        // 3. PreviewFactory validation: creates a temporary preview entity and checks
        //    IsValid(). This runs the game's own 9 validators (terrain, occupancy,
        //    water buildings, district boundaries, etc.)
        // 4. Only after all checks pass: BlockObjectPlacerService.Place() creates the
        //    real building entity in the game world.

        private static int ParseOrientation(string orient) => TimberbotPure.ParseOrientation(orient);

        public PlaceBuildingResult PlaceBuilding(string prefabName, int x, int y, int z, string orientationStr)
        {
            int orientation = ParseOrientation(orientationStr);
            if (orientation < 0)
                return PlaceBuildingResult.Fail("invalid_param: invalid orientation, use south, west, north, east. run: timberbot.py place_building prefab:X x:N y:N z:N orientation:south");

            BuildingSpec buildingSpec;
            try { buildingSpec = _buildingService.GetBuildingTemplate(prefabName); }
            catch { return PlaceBuildingResult.Fail(InvalidPrefabError(prefabName), x, y, z); }
            if (buildingSpec == null)
                return PlaceBuildingResult.Fail(InvalidPrefabError(prefabName), x, y, z);

            var blockObjectSpec = buildingSpec.GetSpec<BlockObjectSpec>();
            if (blockObjectSpec == null)
                return PlaceBuildingResult.Fail("invalid_type: no block object spec", x, y, z);

            // check building is unlocked
            var bs = buildingSpec.GetSpec<BuildingSpec>();
            if (bs != null && bs.ScienceCost > 0 && !_buildingUnlockingService.Unlocked(bs))
                return new PlaceBuildingResult { Error = "not_unlocked: use science/unlock first", X = x, Y = y, Z = z, Prefab = prefabName, ScienceCost = bs.ScienceCost, CurrentPoints = _scienceService.SciencePoints };

            // Origin correction: user always specifies bottom-left corner (smallest x,y).
            var size = blockObjectSpec.Size;
            int rx = size.x, ry = size.y;
            if (orientation == 1 || orientation == 3) { rx = size.y; ry = size.x; }
            int gx = x, gy = y;
            switch (orientation)
            {
                case 1: gy = y + ry - 1; break;
                case 2: gx = x + rx - 1; gy = y + ry - 1; break;
                case 3: gx = x + rx - 1; break;
            }

            int baseZ = blockObjectSpec.BaseZ;
            var validationReason = ValidatePlacement(buildingSpec, blockObjectSpec, x, y, ref z, orientation);
            if (validationReason != null)
                return new PlaceBuildingResult { Error = validationReason, X = x, Y = y, Z = z, Prefab = prefabName };

            var orient = (Timberborn.Coordinates.Orientation)orientation;
            var placement = new Placement(new Vector3Int(gx, gy, z - baseZ), orient, FlipMode.Unflipped);

            var placer = _blockObjectPlacerService.GetMatchingPlacer(blockObjectSpec);
            int placedId = 0;
            string placedName = "";
            placer.Place(blockObjectSpec, placement, (entity) =>
            {
                var ec = entity.GetComponent<EntityComponent>();
                if (ec != null)
                    placedId = _cache.GetLegacyId(ec);
                placedName = TimberbotEntityRegistry.CanonicalName(entity.GameObject.name);
            });

            if (placedId == 0)
                return new PlaceBuildingResult { Error = "operation_failed: placer returned no entity, the game rejected the placement for an unknown reason", X = x, Y = y, Z = z, Prefab = prefabName };

            _readV2.InvalidateBuildings();
            PostLog($"Placed {placedName} at ({x}, {y}, {z})");
            return new PlaceBuildingResult { Id = placedId, Name = placedName, X = x, Y = y, Z = z, Orientation = OrientNames[orientation] };
        }

        private string FindBlockerAt(Vector3Int bc)
        {
            var buildingSnapshot = _readV2.EnsureBuildingsFreshNow(Time.realtimeSinceStartup);
            var naturalSnapshot = _readV2.EnsureNaturalResourcesFreshNow(Time.realtimeSinceStartup);
            for (int i = 0; i < buildingSnapshot.Count; i++)
            {
                var cb = buildingSnapshot.Definitions[i];
                if (cb.OccupiedTiles == null) continue;
                foreach (var t in cb.OccupiedTiles)
                    if (t.x == bc.x && t.y == bc.y) return cb.Name;
            }
            for (int i = 0; i < naturalSnapshot.Count; i++)
            {
                var nd = naturalSnapshot.Definitions[i];
                var nr = naturalSnapshot.States[i];
                if (nr.X == bc.x && nr.Y == bc.y) return nd.Name;
            }
            var blk = _readV2.TrackedBlockers;
            for (int i = 0; i < blk.Count; i++)
            {
                var b = blk[i];
                if (b.OccupiedTiles == null) continue;
                foreach (var t in b.OccupiedTiles)
                    if (t.x == bc.x && t.y == bc.y) return b.Name;
            }
            return null;
        }

        // Returns null if valid, or a reason string if invalid.
        // Iterates game's 9 validators individually to get the specific failure reason.
        private string ValidatePlacement(BuildingSpec buildingSpec, BlockObjectSpec blockObjectSpec, int x, int y, ref int z, int orientation)
        {
            var size = blockObjectSpec.Size;
            int rx = size.x, ry = size.y;
            if (orientation == 1 || orientation == 3) { rx = size.y; ry = size.x; }
            int gx = x, gy = y;
            switch (orientation)
            {
                case 1: gy = y + ry - 1; break;
                case 2: gx = x + rx - 1; gy = y + ry - 1; break;
                case 3: gx = x + rx - 1; break;
            }
            var placeableSpec = buildingSpec.GetSpec<PlaceableBlockObjectSpec>();
            if (placeableSpec == null) return "no placeable spec";
            Preview preview = null;
            try
            {
                int baseZ = blockObjectSpec.BaseZ;
                var placement = new Placement(new Vector3Int(gx, gy, z - baseZ),
                    (Timberborn.Coordinates.Orientation)orientation, FlipMode.Unflipped);
                preview = _previewFactory.Create(placeableSpec);
                preview.Reposition(placement);
                // use service-level validation only, same as the player's PreviewPlacer.PreviewsAreValid
                var validationSvc = preview.BlockObject._blockObjectValidationService;
                if (validationSvc != null && validationSvc.IsValid(preview.BlockObject))
                {
                    var bv2 = preview.BlockObject._blockValidator;
                    if (bv2 != null)
                        foreach (var block in preview.BlockObject.PositionedBlocks.GetAllBlocks())
                        {
                            var bc = block.Coordinates;
                            if (!bv2.FitsInMap(block, false))
                                return $"out of map at ({bc.x},{bc.y},{bc.z}). move placement inside map bounds";
                            if (bv2.BlockConflictsWithExistingObject(block))
                            {
                                string blocker = FindBlockerAt(bc) ?? "unknown";
                                return $"occupied by {blocker} at ({bc.x},{bc.y},{bc.z}). demolish it or try a different location";
                            }
                            if (bv2.BlockConflictsWithTerrain(block))
                                return $"terrain conflict at ({bc.x},{bc.y},{bc.z}). tile is solid rock or wrong elevation, try adjacent tiles or a different z level";
                            if (bv2.BlockConflictsWithBlockAbove(block))
                                return $"blocked above at ({bc.x},{bc.y},{bc.z}). something occupies the tile above, demolish it or use a lower z";
                            if (bv2.BlockConflictsWithBlocksBelow(block))
                                return $"blocked below at ({bc.x},{bc.y},{bc.z}). no support underneath, needs solid ground or a platform below";
                            if (bv2.ConflictsWithUndergroundBlockObject(block))
                                return $"underground conflict at ({bc.x},{bc.y},{bc.z}). underground object blocks this tile, try adjacent tiles";
                            if (bv2.UndergroundBlockIsNotUnderground(block))
                                return $"not underground at ({bc.x},{bc.y},{bc.z}). this building must be placed underground";
                        }
                    z = preview.BlockObject.CoordinatesAtBaseZ.z;
                    return null;
                }

                // invalid. check block-level conflicts first (occupancy, terrain)
                var bv = preview.BlockObject._blockValidator;
                if (bv != null)
                {
                    foreach (var block in preview.BlockObject.PositionedBlocks.GetAllBlocks())
                    {
                        var bc = block.Coordinates;
                        if (bv.BlockConflictsWithExistingObject(block))
                        {
                            string blocker = FindBlockerAt(bc) ?? "unknown";
                            return $"occupied by {blocker} at ({bc.x},{bc.y},{bc.z}). demolish it or try a different location";
                        }
                        if (bv.BlockConflictsWithTerrain(block))
                            return $"terrain conflict at ({bc.x},{bc.y},{bc.z}). tile is solid rock or wrong elevation, try adjacent tiles or a different z level";
                        if (bv.BlockConflictsWithBlocksBelow(block))
                            return $"blocked below at ({bc.x},{bc.y},{bc.z}). no support underneath, needs solid ground or a platform below";
                        if (bv.BlockConflictsWithBlockAbove(block))
                            return $"blocked above at ({bc.x},{bc.y},{bc.z}). something occupies the tile above, demolish it or use a lower z";
                    }
                }

                // check service-level validators (district, water buildings, etc)
                if (validationSvc != null)
                {
                    var validators = validationSvc._blockObjectValidators;
                    for (int i = 0; i < validators.Length; i++)
                    {
                        string reason = null;
                        if (!validators[i].IsValid(preview.BlockObject, out reason))
                            return reason ?? validators[i].GetType().Name;
                    }
                }
                // collect all diagnostic info
                var diag = new System.Text.StringBuilder("placement invalid:");

                // 1. BlockValidator.BlocksValid. what IsValid() actually calls
                bool blocksValidResult = bv != null && bv.BlocksValid(preview.BlockObject.PositionedBlocks);
                diag.Append($" BlocksValid={blocksValidResult}");

                // 2. BlockObjectValidationService.IsValid. what IsValid() actually calls
                bool svcValidResult = validationSvc != null && validationSvc.IsValid(preview.BlockObject);
                diag.Append($" SvcValid={svcValidResult}");

                // 3. per-block details
                foreach (var block in preview.BlockObject.PositionedBlocks.GetAllBlocks())
                {
                    var c = block.Coordinates;
                    int th = GetTerrainHeight(c.x, c.y);
                    float wd = GetWaterDepth(c.x, c.y);
                    var conflicts = new System.Collections.Generic.List<string>();
                    if (bv != null)
                    {
                        if (!bv.FitsInMap(block, false)) conflicts.Add("!fitsInMap");
                        if (bv.BlockConflictsWithExistingObject(block)) conflicts.Add("occupied");
                        if (bv.BlockConflictsWithBlockAbove(block)) conflicts.Add("above");
                        if (bv.BlockConflictsWithBlocksBelow(block)) conflicts.Add("below");
                        if (bv.BlockConflictsWithTerrain(block)) conflicts.Add("terrain");
                        if (bv.ConflictsWithUndergroundBlockObject(block)) conflicts.Add("underground");
                        if (bv.UndergroundBlockIsNotUnderground(block)) conflicts.Add("notUnderground");
                        if (bv.BlockConflictsWithMatterBelow(block, false)) conflicts.Add("matterBelow");
                    }
                    string status = conflicts.Count > 0 ? string.Join("+", conflicts) : "ok";
                    diag.Append($" ({c.x},{c.y},{c.z} t={th} w={wd:F1} {status})");
                }

                // 4. per-validator details
                if (validationSvc != null)
                {
                    var vals = validationSvc._blockObjectValidators;
                    for (int i = 0; i < vals.Length; i++)
                    {
                        string r2 = null;
                        bool ok = vals[i].IsValid(preview.BlockObject, out r2);
                        diag.Append($" {vals[i].GetType().Name}={ok}");
                        if (!ok && r2 != null) diag.Append($"({r2})");
                    }
                }

                return diag.ToString();
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error($"ValidatePlacement at ({x},{y},{z})", ex);
                return ex.Message;
            }
            finally
            {
                if (preview != null)
                    UnityEngine.Object.Destroy(preview.GameObject);
            }
        }
    }
}


