// TimberbotWrite.cs. All state-modifying API endpoints.
//
// POST requests that change game state: speed, workers, priorities, crops, trees,
// stockpiles, floodgates, recipes, science, distribution, migration, work hours.
// Also includes CollectTiles (the map/tiles endpoint) which reads live water state
// and must run on the main thread.
//
// All write methods run on the Unity main thread (queued via DrainRequests).
// They call game services directly (not cached data) and return result objects
// that TimberbotHttpServer serializes to JSON.
//
// Pattern: each method takes primitive params, finds the entity, calls the game
// service, returns {id, name, field: newValue} on success or {error: "code"} on failure.
// Error codes: "code: detail" format (e.g. "not_found", "invalid_type: not a floodgate").
// AI parses the prefix before the colon. Codes: not_found, invalid_type, invalid_param,
// insufficient_science, no_population, operation_failed.

using System;
using System.Collections.Generic;
using System.Globalization;
using Timberborn.EntitySystem;
using Timberborn.BuilderPrioritySystem;
using Timberborn.Buildings;
using Timberborn.BlockObjectTools;
using Timberborn.MapIndexSystem;
using Timberborn.TerrainSystem;
using Timberborn.WaterSystem;
using Timberborn.Forestry;
using Timberborn.Planting;
using Timberborn.GameDistricts;
using Timberborn.InventorySystem;
using Timberborn.PrioritySystem;
using Timberborn.TimeSystem;
using Timberborn.WaterBuildings;
using Timberborn.WorkSystem;
using Timberborn.GameDistrictsMigration;
using Timberborn.ScienceSystem;
using Timberborn.NotificationSystem;
using Timberborn.PowerManagement;
using Timberborn.SoilContaminationSystem;
using Timberborn.EntityNaming;
using Timberborn.Hauling;
using Timberborn.Workshops;
using Timberborn.Fields;
using Timberborn.ToolButtonSystem;
using Timberborn.ToolSystem;
using Timberborn.SoilMoistureSystem;
using Timberborn.NeedSpecs;
using Timberborn.GameFactionSystem;
using Timberborn.StockpilePrioritySystem;
using Timberborn.Emptying;
using UnityEngine;
using Timberborn.Automation;
using Timberborn.AutomationBuildings;

namespace Timberbot
{
    // All POST endpoint handlers that modify game state.
    //
    // These run on the Unity main thread (queued via DrainRequests in TimberbotHttpServer).
    // Each method takes primitive params from the HTTP body, finds the target entity
    // via FindEntity(), calls the game service to make the change, and returns a result
    // object that gets serialized to JSON.
    //
    // Pattern: every write method returns {id, name, field: newValue} on success
    // or {error: "message"} on failure. The HTTP server serializes either to JSON.
    public class TimberbotWrite
    {
        private readonly TimberbotJw _jw = new TimberbotJw(1024);
        // game services for terrain, water, soil (used by tiles endpoint)
        private readonly ITerrainService _terrainService;
        private readonly IThreadSafeWaterMap _waterMap;
        private readonly MapIndexService _mapIndexService;
        private readonly IThreadSafeColumnTerrainMap _terrainMap;
        private readonly ISoilContaminationService _soilContaminationService;
        private readonly ISoilMoistureService _soilMoistureService;
        // game services for write operations
        private readonly SpeedManager _speedManager;
        private readonly RecipeSpecService _recipeSpecService;
        private readonly TreeCuttingArea _treeCuttingArea;
        private readonly PlantingService _plantingService;
        private readonly PlantingAreaValidator _plantingAreaValidator;
        private readonly ScienceService _scienceService;
        private readonly BuildingService _buildingService;
        private readonly BuildingUnlockingService _buildingUnlockingService;
        private readonly ToolButtonService _toolButtonService;
        private readonly ToolUnlockingService _toolUnlockingService;
        private readonly FactionNeedService _factionNeedService;
        private readonly NotificationSaver _notificationSaver;
        private readonly DistrictCenterRegistry _districtCenterRegistry;
        private readonly WorkingHoursManager _workingHoursManager;
        private readonly PopulationDistributorRetriever _populationDistributorRetriever;
        private readonly TimberbotEntityRegistry _cache;
        private readonly TimberbotReadV2 _readv2;
        public bool InGameLoggingEnabled = true;
        public event System.Action<string> OnActionLog;

        public TimberbotWrite(
            ITerrainService terrainService,
            IThreadSafeWaterMap waterMap,
            MapIndexService mapIndexService,
            IThreadSafeColumnTerrainMap terrainMap,
            ISoilContaminationService soilContaminationService,
            ISoilMoistureService soilMoistureService,
            SpeedManager speedManager,
            RecipeSpecService recipeSpecService,
            TreeCuttingArea treeCuttingArea,
            PlantingService plantingService,
            PlantingAreaValidator plantingAreaValidator,
            ScienceService scienceService,
            BuildingService buildingService,
            BuildingUnlockingService buildingUnlockingService,
            ToolButtonService toolButtonService,
            ToolUnlockingService toolUnlockingService,
            FactionNeedService factionNeedService,
            NotificationSaver notificationSaver,
            DistrictCenterRegistry districtCenterRegistry,
            WorkingHoursManager workingHoursManager,
            PopulationDistributorRetriever populationDistributorRetriever,
            TimberbotEntityRegistry cache,
            TimberbotReadV2 readv2)
        {
            _terrainService = terrainService;
            _waterMap = waterMap;
            _mapIndexService = mapIndexService;
            _terrainMap = terrainMap;
            _soilContaminationService = soilContaminationService;
            _soilMoistureService = soilMoistureService;
            _speedManager = speedManager;
            _recipeSpecService = recipeSpecService;
            _treeCuttingArea = treeCuttingArea;
            _plantingService = plantingService;
            _plantingAreaValidator = plantingAreaValidator;
            _scienceService = scienceService;
            _buildingService = buildingService;
            _buildingUnlockingService = buildingUnlockingService;
            _toolButtonService = toolButtonService;
            _toolUnlockingService = toolUnlockingService;
            _factionNeedService = factionNeedService;
            _notificationSaver = notificationSaver;
            _districtCenterRegistry = districtCenterRegistry;
            _workingHoursManager = workingHoursManager;
            _populationDistributorRetriever = populationDistributorRetriever;
            _cache = cache;
            _readv2 = readv2;
        }
        
        private void PostLog(string msg)
        {
            if (!InGameLoggingEnabled) return;
            OnActionLog?.Invoke(msg);
        }

        private static readonly int[] SpeedScale = TimberbotReadV2.SpeedScale;

        // helper: canonical name for error messages (avoids repeating the long call everywhere)
        private static string N(EntityComponent ec) => TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);

        // helper: collect district names for error hints
        private List<string> DistrictNames()
        {
            var names = new List<string>();
            foreach (var dc in _districtCenterRegistry.FinishedDistrictCenters)
                names.Add(dc.DistrictName);
            return names;
        }

        // ================================================================
        // WRITE ENDPOINTS. Tier 1
        // ================================================================

        // game speed 0-3, mapped to internal values 0,1,3,7
        public object SetSpeed(int speed)
        {
            if (speed < 0 || speed > 3)
                return _jw.Error("invalid_param: speed must be 0-3 (0=pause, 1=normal, 2=fast, 3=fastest). run: timberbot.py set_speed speed:1", ("got", speed));

            _speedManager.ChangeSpeed(SpeedScale[speed]);
            PostLog($"Set game speed to {speed}");
            return _jw.Result(("speed", speed));
        }

        // set when beavers stop working (1-24, default 18 = 6pm)
        public object SetWorkHours(int endHours)
        {
            if (endHours < 1 || endHours > 24)
                return _jw.Error("invalid_param: endHours must be 1-24 (hour when work stops, default 18). run: timberbot.py set_workhours end_hours:18", ("got", endHours));
            _workingHoursManager.WorkedPartOfDay = (endHours - _workingHoursManager._startHours) / 24f;
            PostLog($"Set working hours to end at {endHours}:00");
            return _jw.Result(("endHours", (_workingHoursManager.EndHours)));
        }

        // move beavers between districts. requires 2+ districts.
        public object MigratePopulation(string fromDistrict, string toDistrict, int count)
        {
            Timberborn.GameDistricts.DistrictCenter fromDc = null, toDc = null;
            foreach (var dc in _districtCenterRegistry.FinishedDistrictCenters)
            {
                if (dc.DistrictName == fromDistrict) fromDc = dc;
                if (dc.DistrictName == toDistrict) toDc = dc;
            }
            if (fromDc == null) return _jw.Error("not_found: source district does not exist. run: timberbot.py districts to see valid names", ("from", fromDistrict), ("districts", DistrictNames()));
            if (toDc == null) return _jw.Error("not_found: target district does not exist. run: timberbot.py districts to see valid names", ("to", toDistrict), ("districts", DistrictNames()));
            try
            {
                var distributor = _populationDistributorRetriever.GetPopulationDistributor<AdultsDistributorTemplate>(fromDc);
                if (distributor == null)
                    return _jw.Error("not_found: no population distributor", ("from", fromDistrict));
                var available = distributor.Current;
                var toMove = System.Math.Min(count, available);
                if (toMove <= 0)
                    return _jw.Error("no_population: no beavers available to migrate in this district. run: timberbot.py beavers to check population", ("from", fromDistrict), ("available", available), ("requested", count));
                distributor.MigrateTo(toDc, toMove);
                PostLog($"Migrated {toMove} beavers from {fromDistrict} to {toDistrict}");
                return _jw.Result(("from", fromDistrict), ("to", toDistrict), ("migrated", toMove));
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("migration", ex);
                return _jw.Error("operation_failed: " + ex.Message, ("from", fromDistrict), ("to", toDistrict));
            }
        }

        // pause/unpause a building
        public object PauseBuilding(int buildingId, bool paused)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var pausable = ec.GetComponent<PausableBuilding>();
            if (pausable == null)
                return _jw.Error("invalid_type: this building cannot be paused. run: timberbot.py buildings to find pausable buildings", ("id", buildingId), ("name", N(ec)));

            if (paused)
                pausable.Pause();
            else
                pausable.Resume();
            _readv2.InvalidateBuildings();
            PostLog($"{(paused ? "Paused" : "Resumed")} building {buildingId} ({TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)})");
            return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("paused", pausable.Paused).CloseObj().ToString();
        }

        // engage/disengage clutch on a building
        public object SetClutch(int buildingId, bool engaged)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var clutch = ec.GetComponent<Clutch>();
            if (clutch == null)
                return _jw.Error("invalid_type: no clutch. only power-consuming buildings have clutches. run: timberbot.py buildings to find buildings with clutches", ("id", buildingId), ("name", N(ec)));

            clutch.SetMode(engaged ? ClutchMode.Engaged : ClutchMode.Disengaged);
            PostLog($"{(engaged ? "Engaged" : "Disengaged")} clutch on {buildingId}");
            return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("engaged", clutch.IsEngaged).CloseObj().ToString();
        }

        // adjust floodgate water gate height (clamped to max)
        public object SetFloodgateHeight(int buildingId, float height)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var floodgate = ec.GetComponent<Floodgate>();
            if (floodgate == null)
                return _jw.Error("invalid_type: not a floodgate. run: timberbot.py buildings name:Floodgate to find floodgates", ("id", buildingId), ("name", N(ec)));

            var clamped = Mathf.Clamp(height, 0f, floodgate.MaxHeight);
            floodgate.SetHeightAndSynchronize(clamped);
            PostLog($"Set floodgate {buildingId} to {floodgate.Height:F1}");
            return _jw.Result(("id", buildingId), ("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)), ("height", floodgate.Height), ("maxHeight", floodgate.MaxHeight));
        }

        // set construction or workplace priority (VeryLow/Normal/VeryHigh)
        // Buildings have TWO separate priority systems:
        //   "construction".how urgently builders deliver materials and construct it
        //   "workplace".how urgently workers are assigned to it vs other buildings
        // Both use the same VeryLow/Low/Normal/High/VeryHigh enum but are set independently.
        // If type is empty, tries construction first, then workplace.
        public object SetBuildingPriority(int buildingId, string priorityStr, string type)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            if (!Enum.TryParse<Priority>(priorityStr, true, out var parsed))
                return _jw.Error("invalid_param: priority must be one of: VeryLow, Low, Normal, High, VeryHigh. run: timberbot.py set_priority id:N priority:Normal", ("got", priorityStr));

            var bo = ec.GetComponent<Timberborn.BlockSystem.BlockObject>();
            bool finished = bo == null || bo.IsFinished;
            var wpPrio = ec.GetComponent<WorkplacePriority>();
            var builderPrio = ec.GetComponent<BuilderPrioritizable>();

            if (string.IsNullOrEmpty(type))
            {
                type = TimberbotPure.DeterminePriorityToSet(finished, wpPrio != null, builderPrio != null);
            }

            if (type == "workplace" && wpPrio != null)
            {
                wpPrio.SetPriority(parsed);
                PostLog($"Set workplace priority of {buildingId} to {parsed}");
                return _jw.Result(("id", buildingId), ("name", N(ec)), ("workplacePriority", wpPrio.Priority.ToString()));
            }
            if (type == "construction" && builderPrio != null)
            {
                builderPrio.SetPriority(parsed);
                PostLog($"Set construction priority of {buildingId} to {parsed}");
                return _jw.Result(("id", buildingId), ("name", N(ec)), ("constructionPriority", builderPrio.Priority.ToString()));
            }

            return _jw.Error("invalid_type: no priority of that type. type must be 'construction' or 'workplace', or omit for auto-detect. run: timberbot.py set_priority id:N priority:Normal", ("id", buildingId), ("name", N(ec)), ("type", type));
        }

        // haulers deliver goods to this building first
        public object SetHaulPriority(int buildingId, bool prioritized)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var hp = ec.GetComponent<HaulPrioritizable>();
            if (hp == null)
                return _jw.Error("invalid_type: no haul priority. only buildings with inventories support haul priority. run: timberbot.py buildings to find storage/workshop buildings", ("id", buildingId), ("name", N(ec)));

            hp.Prioritized = prioritized;
            PostLog($"{(prioritized ? "Prioritized" : "Unprioritized")} hauling for {buildingId}");
            return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("haulPrioritized", hp.Prioritized).CloseObj().ToString();
        }

        // DANGEROUS: changing a recipe DESTROYS in-progress items and all consumed materials.
        // A BotPartFactory mid-way through a BotChassis will lose the planks, gears, and metal
        // blocks already consumed. Only call this on buildings with no recipe set (new buildings)
        // or when you're certain the current batch is complete.
        // Pass an invalid recipe name to get a list of available recipes in the error response.
        public object SetRecipe(int buildingId, string recipeId)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var manufactory = ec.GetComponent<Manufactory>();
            if (manufactory == null)
                return _jw.Error("invalid_type: no manufactory. only workshops/factories have recipes. run: timberbot.py buildings to find workshops", ("id", buildingId), ("name", N(ec)));

            if (string.IsNullOrEmpty(recipeId) || recipeId == "none")
            {
                if (manufactory.ProductionRecipes == null || manufactory.ProductionRecipes.Length <= 1)
                    return _jw.Error("invalid_type: recipe cannot be cleared. single-recipe buildings always produce. run: timberbot.py buildings id:N detail:full to see recipes", ("id", buildingId), ("name", N(ec)));
                try
                {
                    manufactory.SetRecipe(null);
                    PostLog($"Cleared recipe for {buildingId}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("recipe", "none").CloseObj().ToString();
                }
                catch (System.Exception ex)
                {
                    TimberbotLog.Error("write.recipe.clear", ex);
                    return _jw.Error("operation_failed: " + ex.Message, ("id", buildingId));
                }
            }

            RecipeSpec recipe = null;
            try { recipe = _recipeSpecService.GetRecipe(recipeId); } catch (System.Exception _ex) { TimberbotLog.Error("write", _ex); }
            if (recipe == null)
            {
                var available = new List<string>();
                foreach (var r in manufactory.ProductionRecipes)
                    available.Add(r.Id);
                return _jw.Error("not_found: recipe does not exist for this building. run: timberbot.py buildings id:N detail:full to see available recipes", ("recipeId", recipeId), ("available", available));
            }

            try
            {
                manufactory.SetRecipe(recipe);
                PostLog($"Set recipe for building {buildingId} to {recipe.Id}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("recipe", recipe.Id).CloseObj().ToString();
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("write.recipe.set", ex);
                return _jw.Error("operation_failed: " + ex.Message, ("id", buildingId), ("recipeId", recipeId));
            }
        }

        // prioritize planting vs default (harvest when ready)
        public object SetFarmhouseAction(int buildingId, string action)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var farmhouse = ec.GetComponent<FarmHouse>();
            if (farmhouse == null)
                return _jw.Error("invalid_type: not a farmhouse. run: timberbot.py buildings name:FarmHouse to find farmhouses", ("id", buildingId), ("name", N(ec)));

            if (action == "planting")
            {
                farmhouse.PrioritizePlanting();
                PostLog($"Set farmhouse {buildingId} mode to: Planting");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("action", "planting").CloseObj().ToString();
            }
            else if (action == "harvesting" || action == "none")
            {
                farmhouse.UnprioritizePlanting();
                PostLog($"Set farmhouse {buildingId} mode to: Default");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("action", "default").CloseObj().ToString();
            }

            return _jw.Error("invalid_param: action must be 'planting' or 'harvesting'. run: timberbot.py set_farmhouse_action id:N action:planting", ("got", action));
        }

        // forester/gatherer prioritizes this resource type
        public object SetPlantablePriority(int buildingId, string plantableName)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var prioritizer = ec.GetComponent<PlantablePrioritizer>();
            if (prioritizer == null)
                return _jw.Error("invalid_type: no plantable prioritizer. only foresters and gatherers support this. run: timberbot.py buildings name:Forester to find foresters", ("id", buildingId), ("name", N(ec)));

            if (string.IsNullOrEmpty(plantableName) || plantableName == "none")
            {
                prioritizer.PrioritizePlantable(null);
                PostLog($"Cleared prioritized plantable for {buildingId}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("prioritized", "none").CloseObj().ToString();
            }

            var planterBuilding = ec.GetComponent<PlanterBuilding>();
            if (planterBuilding == null)
                return _jw.Error("invalid_type: no planter component. only foresters and gatherers have plantable lists. run: timberbot.py buildings name:Forester to find foresters", ("id", buildingId), ("name", N(ec)));

            PlantableSpec match = null;
            var available = new List<string>();
            foreach (var p in planterBuilding.AllowedPlantables)
            {
                available.Add(p.TemplateName);
                if (p.TemplateName == plantableName)
                    match = p;
            }

            if (match == null)
                return _jw.Error("not_found: plantable not in this building's list. run: timberbot.py buildings id:N detail:full to see available plantables", ("plantableName", plantableName), ("available", available));

            prioritizer.PrioritizePlantable(match);
            _readv2.InvalidateBuildings();
            PostLog($"Set prioritized plantable for {buildingId} to {match.TemplateName}");
            return _jw.BeginObj().Prop("id", buildingId).Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)).Prop("prioritized", match.TemplateName).CloseObj().ToString();
        }

        // ================================================================
        // WRITE ENDPOINTS. Tier 2
        // ================================================================

        // set desired worker count (0 to maxWorkers)
        public object SetWorkers(int buildingId, int count)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var workplace = ec.GetComponent<Workplace>();
            if (workplace == null)
                return _jw.Error("invalid_type: not a workplace. only staffed buildings (lumberjacks, farms, etc) have workers. run: timberbot.py buildings to find staffed buildings", ("id", buildingId), ("name", N(ec)));

            var clamped = Mathf.Clamp(count, 0, workplace.MaxWorkers);
            workplace.SetDesiredWorkers(clamped);
            _readv2.InvalidateBuildings();
            PostLog($"Set desired workers for {buildingId} to {clamped}");
            return _jw.Result(("id", buildingId), ("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name)), ("desiredWorkers", workplace.DesiredWorkers), ("maxWorkers", workplace.MaxWorkers), ("assignedWorkers", workplace.NumberOfAssignedWorkers));
        }

        // Mark or unmark a rectangular area for tree cutting. Lumberjacks will chop
        // any marked trees within their work range. Uses TreeCuttingArea singleton
        // which is coordinate-based (not per-entity), same system as the player's UI.
        public object MarkCuttingArea(int x1, int y1, int x2, int y2, int z, bool marked)
        {
            var minX = Mathf.Min(x1, x2);
            var maxX = Mathf.Max(x1, x2);
            var minY = Mathf.Min(y1, y2);
            var maxY = Mathf.Max(y1, y2);

            var coords = new List<Vector3Int>();
            for (int x = minX; x <= maxX; x++)
            {
                for (int y = minY; y <= maxY; y++)
                {
                    coords.Add(new Vector3Int(x, y, z));
                }
            }

            if (marked)
                _treeCuttingArea.AddCoordinates(coords);
            else
                _treeCuttingArea.RemoveCoordinates(coords);
            _readv2.InvalidateNaturalResources();
            PostLog($"{(marked ? "Marked" : "Unmarked")} {coords.Count} tiles for tree cutting");

            return new
            {
                x1 = minX,
                y1 = minY,
                x2 = maxX,
                y2 = maxY,
                z,
                marked,
                tiles = coords.Count
            };
        }

        // set storage mode (accept/obtain/supply/empty) and/or allowed good on any storage building
        public object SetStorage(int buildingId, string good, string mode)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var sp = ec.GetComponent<StockpilePriority>();
            if (sp == null)
                return _jw.Error("invalid_type: not a storage building. piles, warehouses, and tanks have storage settings. run: timberbot.py buildings to find storage buildings", ("id", buildingId), ("name", N(ec)));

            // set good if requested
            if (!string.IsNullOrEmpty(good))
            {
                var sga = ec.GetComponent<SingleGoodAllower>();
                if (sga == null)
                    return _jw.Error("invalid_type: this storage does not support good selection. run: timberbot.py buildings name:Warehouse to find configurable storage", ("id", buildingId), ("name", N(ec)));
                if (good == "none")
                    sga.Disallow();
                else
                    sga.Allow(good);
            }

            // set mode if requested
            if (!string.IsNullOrEmpty(mode))
            {
                switch (mode.ToLowerInvariant())
                {
                    case "accept": sp.Accept(); break;
                    case "obtain": sp.Obtain(); break;
                    case "supply": sp.Supply(); break;
                    case "empty": sp.Empty(); break;
                    default:
                        return _jw.Error("invalid_param: mode must be accept, obtain, supply, or empty. run: timberbot.py set_storage id:N mode:accept", ("got", mode));
                }
            }

            // build response with current state
            var name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
            var sga2 = ec.GetComponent<SingleGoodAllower>();
            var currentGood = sga2 != null && sga2.HasAllowedGood ? sga2.AllowedGood : "";
            var currentMode = sp.IsEmptyActive ? "empty" : sp.IsObtainActive ? "obtain" : sp.IsSupplyActive ? "supply" : "accept";
            _readv2.InvalidateBuildings();
            PostLog($"Configured storage {buildingId}: mode={currentMode}, good={currentGood}");
            return _jw.Result(("id", buildingId), ("name", name), ("good", currentGood), ("mode", currentMode));
        }

        // mark area for crop planting (validates via PlantingAreaValidator.CanPlant)
        public object MarkPlanting(int x1, int y1, int x2, int y2, int z, string crop)
        {
            var minX = Mathf.Min(x1, x2);
            var maxX = Mathf.Max(x1, x2);
            var minY = Mathf.Min(y1, y2);
            var maxY = Mathf.Max(y1, y2);

            var coords = new List<Vector3Int>();
            for (int x = minX; x <= maxX; x++)
            {
                for (int y = minY; y <= maxY; y++)
                {
                    coords.Add(new Vector3Int(x, y, z));
                }
            }

            int planted = 0, skipped = 0;
            foreach (var c in coords)
            {
                // PlantingAreaValidator.CanPlant.same check the player UI uses for green/red tiles
                if (!_plantingAreaValidator.CanPlant(c, crop))
                {
                    skipped++;
                    continue;
                }
                _plantingService.SetPlantingCoordinates(c, crop);
                planted++;
            }
            PostLog($"Marked {planted} tiles for planting {crop} (skipped {skipped})");

            return new
            {
                x1 = minX,
                y1 = minY,
                x2 = maxX,
                y2 = maxY,
                z,
                crop,
                planted,
                skipped
            };
        }

        // Find valid planting spots for a crop. Two modes:
        //
        // 1. By building (id != 0): uses InRangePlantingCoordinates to get all
        //    tiles within the farmhouse's work range. Only farmhouses/foresters have this.
        //    The range is a circle around the building, same as the green overlay in-game.
        //
        // 2. By area (x1,y1,x2,y2,z): scans a rectangular region.
        //
        // Each candidate is validated with PlantingAreaValidator.CanPlant(), the same
        // check the player UI uses (green/red tiles). Returns soil moisture and whether
        // a crop is already planted at that spot.
        //
        // Crops need moist soil to grow. During drought, only tiles near standing water
        // stay moist. The AI uses the "moist" field to choose where to plant.
        public object FindPlantingSpots(string crop, int buildingId, int x1, int y1, int x2, int y2, int z)
        {
            if (buildingId != 0)
            {
                var ec = _cache.FindEntity(buildingId);
                if (ec == null) return _jw.Error("not_found: no entity with this id.", ("id", buildingId));
                var inRange = ec.GetComponent<Timberborn.Planting.InRangePlantingCoordinates>();
                if (inRange == null) return _jw.Error("invalid_type: no planting range. only farmhouses and foresters have planting ranges. run: timberbot.py buildings name:FarmHouse to find farmhouses", ("id", buildingId), ("name", N(ec)));

                var jw = _jw.Reset().BeginObj().Prop("crop", crop).Arr("spots");
                foreach (var c in inRange.GetCoordinates())
                {
                    if (!_plantingAreaValidator.CanPlant(c, crop)) continue;
                    jw.OpenObj().Prop("x", c.x).Prop("y", c.y).Prop("z", c.z).Prop("moist", _soilMoistureService.SoilIsMoist(c)).Prop("planted", _plantingService.IsResourceAt(c)).CloseObj();
                }
                jw.CloseArr().CloseObj();
                return jw.ToString();
            }
            else
            {
                var jw = _jw.Reset().BeginObj().Prop("crop", crop).Arr("spots");
                for (int x = Mathf.Min(x1, x2); x <= Mathf.Max(x1, x2); x++)
                    for (int y = Mathf.Min(y1, y2); y <= Mathf.Max(y1, y2); y++)
                    {
                        var c = new Vector3Int(x, y, z);
                        if (!_plantingAreaValidator.CanPlant(c, crop)) continue;
                        jw.OpenObj().Prop("x", x).Prop("y", y).Prop("z", z).Prop("moist", _soilMoistureService.SoilIsMoist(c)).Prop("planted", _plantingService.IsResourceAt(c)).CloseObj();
                    }
                jw.CloseArr().CloseObj();
                return jw.ToString();
            }
        }

        internal ITimberbotWriteJob CreateFindPlantingSpotsJob(string crop, int buildingId, int x1, int y1, int x2, int y2, int z)
            => new FindPlantingSpotsJob(this, crop, buildingId, x1, y1, x2, y2, z);

        public object UnmarkPlanting(int x1, int y1, int x2, int y2, int z)
        {
            var minX = Mathf.Min(x1, x2);
            var maxX = Mathf.Max(x1, x2);
            var minY = Mathf.Min(y1, y2);
            var maxY = Mathf.Max(y1, y2);

            var coords = new List<Vector3Int>();
            for (int x = minX; x <= maxX; x++)
            {
                for (int y = minY; y <= maxY; y++)
                {
                    coords.Add(new Vector3Int(x, y, z));
                }
            }

            foreach (var c in coords)
            {
                _plantingService.UnsetPlantingCoordinates(c);
            }
            PostLog($"Cleared planting zone for {coords.Count} tiles");

            return new
            {
                x1 = minX,
                y1 = minY,
                x2 = maxX,
                y2 = maxY,
                z,
                cleared = true,
                tiles = coords.Count
            };
        }

        // Unlock a building using science points. Matches the exact UI flow when a
        // player clicks "Unlock" in the science panel: checks cost, deducts points,
        // fires events, and updates the UI toolbar.
        //
        // We iterate ToolButtons (the building toolbar) rather than BuildingService
        // because ToolUnlockingService.UnlockInternal requires the BlockObjectTool
        // reference to update the toolbar state. Without this, the building would be
        // unlocked internally but the toolbar button would still show as locked.
        public object UnlockBuilding(string buildingName)
        {
            try
            {
                foreach (var toolButton in _toolButtonService.ToolButtons)
                {
                    // only BlockObjectTool entries are buildings (others are path tool, demolish, etc)
                    var blockObjectTool = toolButton.Tool as BlockObjectTool;
                    if (blockObjectTool == null) continue;
                    var templateSpec = blockObjectTool.Template.GetSpec<Timberborn.TemplateSystem.TemplateSpec>();
                    if (templateSpec != null && templateSpec.TemplateName == buildingName)
                    {
                        var buildingSpec = blockObjectTool.Template.GetSpec<BuildingSpec>();
                        if (buildingSpec != null && _buildingUnlockingService.Unlocked(buildingSpec))
                            return _jw.Result(("building", buildingName), ("unlocked", true), ("remaining", _scienceService.SciencePoints), ("note", "already unlocked"));
                        var cost = buildingSpec?.ScienceCost ?? 0;
                        if (cost > _scienceService.SciencePoints)
                            return _jw.Error("insufficient_science: not enough science points to unlock. run: timberbot.py science to check current points", ("building", buildingName), ("scienceCost", cost), ("currentPoints", _scienceService.SciencePoints));
                        _buildingUnlockingService.Unlock(buildingSpec);
                        _toolUnlockingService.UnlockInternal(blockObjectTool, () => { });
                        PostLog($"Unlocked building: {buildingName}");
                        return _jw.Result(("building", buildingName), ("unlocked", true), ("remaining", _scienceService.SciencePoints));
                    }
                }

                return _jw.Error("not_found: building not in toolbar. run: timberbot.py prefabs to list all building names", ("building", buildingName));
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("unlock", ex);
                return _jw.Error("operation_failed: " + ex.Message, ("building", buildingName));
            }
        }

        // Set import/export settings for a good in a district.
        // Timberborn's distribution system controls how goods flow between districts:
        //   ImportOption: Auto, Forced (Forced = always import even if local stock is ok)
        //   ExportThreshold: export excess above this amount to other districts
        // Pass "" (or omit) for importOption to leave it unchanged.
        // -1 for exportThreshold means "don't change" (only update import option).
        public object SetDistribution(string districtName, string goodId, string importOption, int exportThreshold)
        {
            foreach (var dc in _districtCenterRegistry.FinishedDistrictCenters)
            {
                if (dc.DistrictName != districtName) continue;

                var distSetting = dc.GetComponent<Timberborn.DistributionSystem.DistrictDistributionSetting>();
                if (distSetting == null)
                    return _jw.Error("invalid_type: district has no distribution settings. run: timberbot.py distribution to see district distribution state", ("district", districtName));

                try
                {
                    var gs = distSetting.GetGoodDistributionSetting(goodId);
                    if (gs != null)
                    {
                        if (!string.IsNullOrEmpty(importOption) &&
                            Enum.TryParse<Timberborn.DistributionSystem.ImportOption>(importOption, true, out var parsed))
                            gs.SetImportOption(parsed);
                        if (exportThreshold >= 0)
                            gs.SetExportThreshold(exportThreshold);
                    }
                }
                catch (System.Exception ex)
                {
                    TimberbotLog.Error("distribution", ex);
                    return _jw.Error("operation_failed: " + ex.Message, ("district", districtName), ("good", goodId));
                }

                PostLog($"Updated distribution for {goodId} in {districtName}");
                return _jw.Result(("district", districtName), ("good", goodId), ("importOption", importOption), ("exportThreshold", exportThreshold));
            }
            return _jw.Error("not_found: district does not exist. run: timberbot.py districts to see valid names", ("district", districtName), ("districts", DistrictNames()));
        }

        // Get the work range for a building (farmhouse, lumberjack, forester, gatherer).
        // Returns the list of tiles this building's workers can reach.same green circle
        // the player sees in the UI when selecting the building. Also counts how many
        // tiles have moist soil (important for crop placement near water).
        public object CollectBuildingRange(int buildingId)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var terrainRange = ec.GetComponent<Timberborn.BuildingsNavigation.BuildingTerrainRange>();
            if (terrainRange == null)
                return _jw.Error("invalid_type: no work range. only farmhouses, lumberjacks, foresters, gatherers, and scavengers have work ranges. run: timberbot.py buildings to find ranged buildings", ("id", buildingId), ("name", N(ec)));

            var range = terrainRange.GetRange();
            int moistCount = 0;
            int minX = int.MaxValue, minY = int.MaxValue, maxX = int.MinValue, maxY = int.MinValue;
            foreach (var c in range)
            {
                if (c.x < minX) minX = c.x;
                if (c.x > maxX) maxX = c.x;
                if (c.y < minY) minY = c.y;
                if (c.y > maxY) maxY = c.y;
                if (_soilMoistureService.SoilIsMoist(c)) moistCount++;
            }

            return new
            {
                id = buildingId,
                name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name),
                tiles = range.Count,
                moist = moistCount,
                bounds = range.Count > 0 ? new { x1 = minX, y1 = minY, x2 = maxX, y2 = maxY } : null
            };
        }

        internal ITimberbotWriteJob CreateCollectBuildingRangeJob(int buildingId)
            => new CollectBuildingRangeJob(this, buildingId);

        private sealed class FindPlantingSpotsJob : ITimberbotWriteJob
        {
            private readonly TimberbotWrite _owner;
            private readonly string _crop;
            private readonly int _buildingId;
            private readonly int _x1;
            private readonly int _y1;
            private readonly int _x2;
            private readonly int _y2;
            private readonly int _z;
            private readonly List<Spot> _spots = new List<Spot>();
            private List<Vector3Int> _coords;
            private int _index;
            private bool _initialized;
            private bool _completed;
            private int _statusCode = 200;
            private object _result;

            private struct Spot
            {
                public int X;
                public int Y;
                public int Z;
                public bool Moist;
                public bool Planted;
            }

            public FindPlantingSpotsJob(TimberbotWrite owner, string crop, int buildingId, int x1, int y1, int x2, int y2, int z)
            {
                _owner = owner;
                _crop = crop;
                _buildingId = buildingId;
                _x1 = x1;
                _y1 = y1;
                _x2 = x2;
                _y2 = y2;
                _z = z;
            }

            public string Name => "/api/planting/find";
            public bool IsCompleted => _completed;
            public int StatusCode => _statusCode;
            public object Result => _result;

            public void Step(float now, double budgetMs)
            {
                if (_completed) return;
                if (!_initialized && !Initialize()) return;

                var sw = System.Diagnostics.Stopwatch.StartNew();
                while (_index < _coords.Count)
                {
                    var c = _coords[_index++];
                    if (_owner._plantingAreaValidator.CanPlant(c, _crop))
                    {
                        _spots.Add(new Spot
                        {
                            X = c.x,
                            Y = c.y,
                            Z = c.z,
                            Moist = _owner._soilMoistureService.SoilIsMoist(c),
                            Planted = _owner._plantingService.IsResourceAt(c)
                        });
                    }

                    if (sw.Elapsed.TotalMilliseconds >= budgetMs)
                        return;
                }

                var jw = _owner._jw.Reset().BeginObj().Prop("crop", _crop).Arr("spots");
                for (int i = 0; i < _spots.Count; i++)
                {
                    var s = _spots[i];
                    jw.OpenObj()
                        .Prop("x", s.X)
                        .Prop("y", s.Y)
                        .Prop("z", s.Z)
                        .Prop("moist", s.Moist)
                        .Prop("planted", s.Planted)
                        .CloseObj();
                }
                _result = jw.CloseArr().CloseObj().ToString();
                _completed = true;
            }

            public void Cancel(string error)
            {
                if (_completed) return;
                _statusCode = 500;
                _result = "{\"error\":\"" + error.Replace("\"", "'") + "\"}";
                _completed = true;
            }

            private bool Initialize()
            {
                _initialized = true;
                _coords = new List<Vector3Int>();

                if (_buildingId != 0)
                {
                    var ec = _owner._cache.FindEntity(_buildingId);
                    if (ec == null)
                    {
                        _result = _owner._jw.Error("not_found: no entity with this id.", ("id", _buildingId));
                        _completed = true;
                        return false;
                    }

                    var inRange = ec.GetComponent<Timberborn.Planting.InRangePlantingCoordinates>();
                    if (inRange == null)
                    {
                        _result = _owner._jw.Error("invalid_type: no planting range. only farmhouses and foresters have planting ranges. run: timberbot.py buildings name:FarmHouse to find farmhouses", ("id", _buildingId), ("name", N(ec)));
                        _completed = true;
                        return false;
                    }

                    foreach (var c in inRange.GetCoordinates())
                        _coords.Add(c);
                    return true;
                }

                for (int x = Mathf.Min(_x1, _x2); x <= Mathf.Max(_x1, _x2); x++)
                    for (int y = Mathf.Min(_y1, _y2); y <= Mathf.Max(_y1, _y2); y++)
                        _coords.Add(new Vector3Int(x, y, _z));
                return true;
            }
        }

        private sealed class CollectBuildingRangeJob : ITimberbotWriteJob
        {
            private readonly TimberbotWrite _owner;
            private readonly int _buildingId;
            private List<Vector3Int> _range;
            private string _name;
            private int _index;
            private int _moistCount;
            private int _minX = int.MaxValue;
            private int _minY = int.MaxValue;
            private int _maxX = int.MinValue;
            private int _maxY = int.MinValue;
            private bool _initialized;
            private bool _completed;
            private int _statusCode = 200;
            private object _result;

            public CollectBuildingRangeJob(TimberbotWrite owner, int buildingId)
            {
                _owner = owner;
                _buildingId = buildingId;
            }

            public string Name => "/api/building/range";
            public bool IsCompleted => _completed;
            public int StatusCode => _statusCode;
            public object Result => _result;

            public void Step(float now, double budgetMs)
            {
                if (_completed) return;
                if (!_initialized && !Initialize()) return;

                var sw = System.Diagnostics.Stopwatch.StartNew();
                while (_index < _range.Count)
                {
                    var c = _range[_index++];
                    if (c.x < _minX) _minX = c.x;
                    if (c.x > _maxX) _maxX = c.x;
                    if (c.y < _minY) _minY = c.y;
                    if (c.y > _maxY) _maxY = c.y;
                    if (_owner._soilMoistureService.SoilIsMoist(c)) _moistCount++;

                    if (sw.Elapsed.TotalMilliseconds >= budgetMs)
                        return;
                }

                _result = new
                {
                    id = _buildingId,
                    name = _name,
                    tiles = _range.Count,
                    moist = _moistCount,
                    bounds = _range.Count > 0 ? new { x1 = _minX, y1 = _minY, x2 = _maxX, y2 = _maxY } : null
                };
                _completed = true;
            }

            public void Cancel(string error)
            {
                if (_completed) return;
                _statusCode = 500;
                _result = "{\"error\":\"" + error.Replace("\"", "'") + "\"}";
                _completed = true;
            }

            private bool Initialize()
            {
                _initialized = true;
                var ec = _owner._cache.FindEntity(_buildingId);
                if (ec == null)
                {
                    _result = _owner._jw.Error("not_found: no entity with this id.", ("id", _buildingId));
                    _completed = true;
                    return false;
                }

                var terrainRange = ec.GetComponent<Timberborn.BuildingsNavigation.BuildingTerrainRange>();
                if (terrainRange == null)
                {
                    _result = _owner._jw.Error("invalid_type: no work range. only farmhouses, lumberjacks, foresters, gatherers, and scavengers have work ranges. run: timberbot.py buildings to find ranged buildings", ("id", _buildingId), ("name", N(ec)));
                    _completed = true;
                    return false;
                }

                _range = new List<Vector3Int>();
                foreach (var c in terrainRange.GetRange())
                    _range.Add(c);
                _name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
                return true;
            }
        }

        // ================================================================
        // AUTOMATION WRITE ENDPOINTS
        // ================================================================

        // Wire a sensor/relay output to a building's input.
        public object LinkAutomation(int sourceId, int targetId, string input)
        {
            var source = _cache.FindEntity(sourceId);
            if (source == null)
                return _jw.Error("not_found: no entity with this id.", ("id", sourceId));

            var target = _cache.FindEntity(targetId);
            if (target == null)
                return _jw.Error("not_found: no entity with this id.", ("id", targetId));

            var sourceAutomator = source.GetComponent<Automator>();
            if (sourceAutomator == null || !sourceAutomator.IsTransmitter)
                return _jw.Error("invalid_type: source is not a transmitter", ("id", sourceId), ("name", N(source)));

            var inputLower = input?.ToLowerInvariant();
            var automatable = target.GetComponent<Automatable>();
            if (automatable != null)
            {
                automatable.SetInput(sourceAutomator);
                PostLog($"Linked {sourceId} to input A of {targetId}");
                return _jw.BeginObj()
                    .Prop("id", targetId)
                    .Prop("name", N(target))
                    .Prop("input", "a")  // Automatable has only one input slot
                    .Prop("sourceId", sourceId)
                    .Prop("sourceName", N(source))
                    .Prop("connected", true)
                    .CloseObj().ToString();
            }

            var relay = target.GetComponent<Relay>();
            if (relay != null)
            {
                if (inputLower != "a" && inputLower != "b")
                    return _jw.Error("invalid_param: input must be a or b for Relay", ("got", input));
                if (inputLower == "b" && !relay.UsesInputB)
                    return _jw.Error("invalid_param: Relay mode " + relay.Mode + " does not use input B. Change mode first with configure_automation property:mode", ("mode", relay.Mode.ToString()), ("availableModes", "And, Or, Xor"));
                if (inputLower == "a")
                    relay.SetInputA(sourceAutomator);
                else
                    relay.SetInputB(sourceAutomator);
                PostLog($"Linked {sourceId} to input {input} of relay {targetId}");
                return _jw.BeginObj()
                    .Prop("id", targetId)
                    .Prop("name", N(target))
                    .Prop("input", input)
                    .Prop("sourceId", sourceId)
                    .Prop("sourceName", N(source))
                    .Prop("connected", true)
                    .CloseObj().ToString();
            }

            var memory = target.GetComponent<Memory>();
            if (memory != null)
            {
                if (inputLower != "a" && inputLower != "b" && inputLower != "reset")
                    return _jw.Error("invalid_param: input must be a, b, or reset for Memory", ("got", input));
                if (inputLower == "b" && !memory.UsesInputB)
                    return _jw.Error("invalid_param: Memory mode " + memory.Mode + " does not use input B. Change mode first with configure_automation property:mode", ("mode", memory.Mode.ToString()), ("availableModes", "Latch, FlipFlop"));
                if (inputLower == "a")
                    memory.SetInputA(sourceAutomator);
                else if (inputLower == "b")
                    memory.SetInputB(sourceAutomator);
                else
                    memory.SetResetInput(sourceAutomator);
                PostLog($"Linked {sourceId} to input {input} of memory {targetId}");
                return _jw.BeginObj()
                    .Prop("id", targetId)
                    .Prop("name", N(target))
                    .Prop("input", input)
                    .Prop("sourceId", sourceId)
                    .Prop("sourceName", N(source))
                    .Prop("connected", true)
                    .CloseObj().ToString();
            }

            return _jw.Error("invalid_type: target cannot accept automation input", ("id", targetId), ("name", N(target)));
        }

        // Disconnect a wiring input from a building.
        public object UnlinkAutomation(int targetId, string input)
        {
            var target = _cache.FindEntity(targetId);
            if (target == null)
                return _jw.Error("not_found: no entity with this id.", ("id", targetId));

            var inputLower = input?.ToLowerInvariant();

            int oldSrcId = 0;
            string oldSrcName = "";
            void CaptureOldSource(Automator aut)
            {
                if (aut == null) return;
                var ec = aut.GetComponent<EntityComponent>();
                if (ec == null) return;
                oldSrcId = _cache.GetLegacyId(ec);
                oldSrcName = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
            }

            var automatable = target.GetComponent<Automatable>();
            if (automatable != null)
            {
                CaptureOldSource(automatable.Input);
                if (automatable.IsAutomated)
                    automatable._inputConnection.Disconnect();
                PostLog($"Disconnected input A of {targetId}");
                return _jw.BeginObj()
                    .Prop("id", targetId)
                    .Prop("name", N(target))
                    .Prop("input", "a")
                    .Prop("connected", false)
                    .Prop("sourceId", oldSrcId)
                    .Prop("sourceName", oldSrcName)
                    .CloseObj().ToString();
            }

            var relay = target.GetComponent<Relay>();
            if (relay != null)
            {
                if (inputLower != "a" && inputLower != "b")
                    return _jw.Error("invalid_param: input must be a or b for Relay", ("got", input));
                if (inputLower == "b" && !relay.UsesInputB)
                    return _jw.Error("invalid_param: Relay mode " + relay.Mode + " does not use input B. Change mode first with configure_automation property:mode", ("mode", relay.Mode.ToString()), ("availableModes", "And, Or, Xor"));
                
                CaptureOldSource(inputLower == "a" ? relay.InputA : relay.InputB);
                if (inputLower == "a")
                    relay.SetInputA(null);
                else
                    relay.SetInputB(null);
                PostLog($"Disconnected input {input} of relay {targetId}");
                return _jw.BeginObj()
                    .Prop("id", targetId)
                    .Prop("name", N(target))
                    .Prop("input", input)
                    .Prop("connected", false)
                    .Prop("sourceId", oldSrcId)
                    .Prop("sourceName", oldSrcName)
                    .CloseObj().ToString();
            }

            var memory = target.GetComponent<Memory>();
            if (memory != null)
            {
                if (inputLower != "a" && inputLower != "b" && inputLower != "reset")
                    return _jw.Error("invalid_param: input must be a, b, or reset for Memory", ("got", input));
                if (inputLower == "b" && !memory.UsesInputB)
                    return _jw.Error("invalid_param: Memory mode " + memory.Mode + " does not use input B. Change mode first with configure_automation property:mode", ("mode", memory.Mode.ToString()), ("availableModes", "Latch, FlipFlop"));
                
                CaptureOldSource(inputLower == "a" ? memory.InputA : inputLower == "b" ? memory.InputB : memory.ResetInput);
                if (inputLower == "a")
                    memory.SetInputA(null);
                else if (inputLower == "b")
                    memory.SetInputB(null);
                else
                    memory.SetResetInput(null);
                PostLog($"Disconnected input {input} of memory {targetId}");
                return _jw.BeginObj()
                    .Prop("id", targetId)
                    .Prop("name", N(target))
                    .Prop("input", input)
                    .Prop("connected", false)
                    .Prop("sourceId", oldSrcId)
                    .Prop("sourceName", oldSrcName)
                    .CloseObj().ToString();
            }

            return _jw.Error("invalid_type: target has no automation input", ("id", targetId), ("name", N(target)));
        }

        // Set a property on an automation component.
        public object ConfigureAutomation(int buildingId, string property, string value)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: no entity with this id.", ("id", buildingId));

            var depthSensor = ec.GetComponent<DepthSensor>();
            if (depthSensor != null)
            {
                if (property == "threshold")
                {
                    if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var threshold))
                        return _jw.Error("invalid_param: threshold must be a number", ("value", value));
                    depthSensor.SetThreshold(threshold);
                    PostLog($"Configured DepthSensor {buildingId} threshold = {threshold}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", N(ec)).Prop("property", property).Prop("value", threshold).Prop("automationType", "DepthSensor").CloseObj().ToString();
                }
                if (property == "mode")
                {
                    if (!Enum.TryParse<NumericComparisonMode>(value, true, out var mode))
                        return _jw.Error("invalid_param: mode must be one of: Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual", ("got", value));
                    depthSensor.SetMode(mode);
                    PostLog($"Configured DepthSensor {buildingId} mode = {mode}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", N(ec)).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "DepthSensor").CloseObj().ToString();
                }
                return _jw.Error("invalid_param: unknown property for DepthSensor", ("property", property), ("available", "threshold, mode"));
            }

            var contaminationSensor = ec.GetComponent<ContaminationSensor>();
            if (contaminationSensor != null)
            {
                if (property == "threshold")
                {
                    if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var threshold))
                        return _jw.Error("invalid_param: threshold must be a number", ("value", value));
                    contaminationSensor.SetThreshold(threshold);
                    PostLog($"Configured ContaminationSensor {buildingId} threshold = {threshold}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", N(ec)).Prop("property", property).Prop("value", threshold).Prop("automationType", "ContaminationSensor").CloseObj().ToString();
                }
                if (property == "mode")
                {
                    if (!Enum.TryParse<NumericComparisonMode>(value, true, out var mode))
                        return _jw.Error("invalid_param: mode must be one of: Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual", ("got", value));
                    contaminationSensor.SetMode(mode);
                    PostLog($"Configured ContaminationSensor {buildingId} mode = {mode}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", N(ec)).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "ContaminationSensor").CloseObj().ToString();
                }
                return _jw.Error("invalid_param: unknown property for ContaminationSensor", ("property", property), ("available", "threshold, mode"));
            }

            var flowSensor = ec.GetComponent<FlowSensor>();
            if (flowSensor != null)
            {
                if (property == "threshold")
                {
                    if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var threshold))
                        return _jw.Error("invalid_param: threshold must be a number", ("value", value));
                    flowSensor.SetThreshold(threshold);
                    PostLog($"Configured FlowSensor {buildingId} threshold = {threshold}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", N(ec)).Prop("property", property).Prop("value", threshold).Prop("automationType", "FlowSensor").CloseObj().ToString();
                }
                if (property == "mode")
                {
                    if (!Enum.TryParse<NumericComparisonMode>(value, true, out var mode))
                        return _jw.Error("invalid_param: mode must be one of: Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual", ("got", value));
                    flowSensor.SetMode(mode);
                    PostLog($"Configured FlowSensor {buildingId} mode = {mode}");
                    return _jw.BeginObj().Prop("id", buildingId).Prop("name", N(ec)).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "FlowSensor").CloseObj().ToString();
                }
                return _jw.Error("invalid_param: unknown property for FlowSensor", ("property", property), ("available", "threshold, mode"));
            }

            var resourceCounter = ec.GetComponent<ResourceCounter>();
            if (resourceCounter != null)
                return ConfigureResourceCounter(resourceCounter, property, value, buildingId, N(ec));

            var populationCounter = ec.GetComponent<PopulationCounter>();
            if (populationCounter != null)
                return ConfigurePopulationCounter(populationCounter, property, value, buildingId, N(ec));

            var powerMeter = ec.GetComponent<PowerMeter>();
            if (powerMeter != null)
                return ConfigurePowerMeter(powerMeter, property, value, buildingId, N(ec));

            var relay = ec.GetComponent<Relay>();
            if (relay != null)
                return ConfigureRelay(relay, property, value, buildingId, N(ec));

            var memory = ec.GetComponent<Memory>();
            if (memory != null)
                return ConfigureMemory(memory, property, value, buildingId, N(ec));

            var chronometer = ec.GetComponent<Chronometer>();
            if (chronometer != null)
                return ConfigureChronometer(chronometer, property, value, buildingId, N(ec));

            var lever = ec.GetComponent<Lever>();
            if (lever != null)
                return ConfigureLever(lever, property, value, buildingId, N(ec));

            return _jw.Error("invalid_type: building has no automation component", ("id", buildingId), ("name", N(ec)));
        }

        private object ConfigureResourceCounter(ResourceCounter component, string property, string value, int buildingId, string name)
        {
            if (property == "goodId")
            {
                component.SetGoodId(value);
                PostLog($"Configured ResourceCounter {buildingId} goodId = {value}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", value).Prop("automationType", "ResourceCounter").CloseObj().ToString();
            }
            if (property == "threshold")
            {
                if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var threshold))
                    return _jw.Error("invalid_param: threshold must be an integer", ("value", value));
                component.SetThreshold(threshold);
                PostLog($"Configured ResourceCounter {buildingId} threshold = {threshold}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", threshold).Prop("automationType", "ResourceCounter").CloseObj().ToString();
            }
            if (property == "fillRateThreshold")
            {
                if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var frThreshold))
                    return _jw.Error("invalid_param: fillRateThreshold must be a number", ("value", value));
                component.SetFillRateThreshold(frThreshold);
                PostLog($"Configured ResourceCounter {buildingId} fillRateThreshold = {frThreshold}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", frThreshold).Prop("automationType", "ResourceCounter").CloseObj().ToString();
            }
            if (property == "mode")
            {
                if (!Enum.TryParse<ResourceCounterMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: mode must be StockLevel or FillRate", ("got", value));
                component.SetMode(mode);
                PostLog($"Configured ResourceCounter {buildingId} mode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "ResourceCounter").CloseObj().ToString();
            }
            if (property == "comparisonMode")
            {
                if (!Enum.TryParse<NumericComparisonMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: comparisonMode must be one of: Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual", ("got", value));
                component.SetComparisonMode(mode);
                PostLog($"Configured ResourceCounter {buildingId} comparisonMode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "ResourceCounter").CloseObj().ToString();
            }
            if (property == "includeInputs")
            {
                if (!bool.TryParse(value, out var include))
                    return _jw.Error("invalid_param: includeInputs must be true or false", ("value", value));
                component.SetIncludeInputs(include);
                PostLog($"Configured ResourceCounter {buildingId} includeInputs = {include}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", include).Prop("automationType", "ResourceCounter").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for ResourceCounter", ("property", property), ("available", "goodId, threshold, fillRateThreshold, mode, comparisonMode, includeInputs"));
        }

        private object ConfigurePopulationCounter(PopulationCounter component, string property, string value, int buildingId, string name)
        {
            if (property == "threshold")
            {
                if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var threshold))
                    return _jw.Error("invalid_param: threshold must be an integer", ("value", value));
                component.SetThreshold(threshold);
                PostLog($"Configured PopulationCounter {buildingId} threshold = {threshold}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", threshold).Prop("automationType", "PopulationCounter").CloseObj().ToString();
            }
            if (property == "mode")
            {
                if (!Enum.TryParse<PopulationCounterMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: mode must be Greater, Less, GreaterOrEqual, or LessOrEqual", ("got", value));
                component.SetMode(mode);
                PostLog($"Configured PopulationCounter {buildingId} mode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "PopulationCounter").CloseObj().ToString();
            }
            if (property == "comparisonMode")
            {
                if (!Enum.TryParse<NumericComparisonMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: comparisonMode must be one of: Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual", ("got", value));
                component.SetComparisonMode(mode);
                PostLog($"Configured PopulationCounter {buildingId} comparisonMode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "PopulationCounter").CloseObj().ToString();
            }
            if (property == "globalMode")
            {
                if (!bool.TryParse(value, out var global))
                    return _jw.Error("invalid_param: globalMode must be true or false", ("value", value));
                component.SetGlobalMode(global);
                PostLog($"Configured PopulationCounter {buildingId} globalMode = {global}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", global).Prop("automationType", "PopulationCounter").CloseObj().ToString();
            }
            if (property == "countBeavers")
            {
                if (!bool.TryParse(value, out var count))
                    return _jw.Error("invalid_param: countBeavers must be true or false", ("value", value));
                component.SetCountBeavers(count);
                PostLog($"Configured PopulationCounter {buildingId} countBeavers = {count}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", count).Prop("automationType", "PopulationCounter").CloseObj().ToString();
            }
            if (property == "countBots")
            {
                if (!bool.TryParse(value, out var count))
                    return _jw.Error("invalid_param: countBots must be true or false", ("value", value));
                component.SetCountBots(count);
                PostLog($"Configured PopulationCounter {buildingId} countBots = {count}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", count).Prop("automationType", "PopulationCounter").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for PopulationCounter", ("property", property), ("available", "threshold, mode, comparisonMode, globalMode, countBeavers, countBots"));
        }

        private object ConfigurePowerMeter(PowerMeter component, string property, string value, int buildingId, string name)
        {
            if (property == "mode")
            {
                if (!Enum.TryParse<PowerMeterMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: mode must be Power or Percent", ("got", value));
                component.SetMode(mode);
                PostLog($"Configured PowerMeter {buildingId} mode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "PowerMeter").CloseObj().ToString();
            }
            if (property == "comparisonMode")
            {
                if (!Enum.TryParse<NumericComparisonMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: comparisonMode must be one of: Equal, NotEqual, Greater, GreaterOrEqual, Less, LessOrEqual", ("got", value));
                component.SetComparisonMode(mode);
                PostLog($"Configured PowerMeter {buildingId} comparisonMode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "PowerMeter").CloseObj().ToString();
            }
            if (property == "intThreshold")
            {
                if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var threshold))
                    return _jw.Error("invalid_param: intThreshold must be an integer", ("value", value));
                component.SetIntThreshold(threshold);
                PostLog($"Configured PowerMeter {buildingId} intThreshold = {threshold}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", threshold).Prop("automationType", "PowerMeter").CloseObj().ToString();
            }
            if (property == "percentThreshold")
            {
                if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var pctThreshold))
                    return _jw.Error("invalid_param: percentThreshold must be a number", ("value", value));
                component.SetPercentThreshold(pctThreshold);
                PostLog($"Configured PowerMeter {buildingId} percentThreshold = {pctThreshold}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", pctThreshold).Prop("automationType", "PowerMeter").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for PowerMeter", ("property", property), ("available", "mode, comparisonMode, intThreshold, percentThreshold"));
        }

        private object ConfigureRelay(Relay component, string property, string value, int buildingId, string name)
        {
            if (property == "mode")
            {
                if (!Enum.TryParse<RelayMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: mode must be Not, And, Or, Xor, or Passthrough", ("got", value));
                component.SetMode(mode);
                PostLog($"Configured Relay {buildingId} mode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "Relay").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for Relay", ("property", property), ("available", "mode"));
        }

        private object ConfigureMemory(Memory component, string property, string value, int buildingId, string name)
        {
            if (property == "mode")
            {
                if (!Enum.TryParse<MemoryMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: mode must be SetReset, Toggle, Latch, or FlipFlop", ("got", value));
                component.SetMode(mode);
                PostLog($"Configured Memory {buildingId} mode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "Memory").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for Memory", ("property", property), ("available", "mode"));
        }

        private object ConfigureChronometer(Chronometer component, string property, string value, int buildingId, string name)
        {
            if (property == "startTime")
            {
                if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var startTime))
                    return _jw.Error("invalid_param: startTime must be a number", ("value", value));
                component.SetStartTime(startTime);
                PostLog($"Configured Chronometer {buildingId} startTime = {startTime}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", startTime).Prop("automationType", "Chronometer").CloseObj().ToString();
            }
            if (property == "endTime")
            {
                if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var endTime))
                    return _jw.Error("invalid_param: endTime must be a number", ("value", value));
                component.SetEndTime(endTime);
                PostLog($"Configured Chronometer {buildingId} endTime = {endTime}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", endTime).Prop("automationType", "Chronometer").CloseObj().ToString();
            }
            if (property == "mode")
            {
                if (!Enum.TryParse<ChronometerMode>(value, true, out var mode))
                    return _jw.Error("invalid_param: mode must be TimeRange, WorkingHours, or NonWorkingHours", ("got", value));
                component.SetMode(mode);
                PostLog($"Configured Chronometer {buildingId} mode = {mode}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", mode.ToString()).Prop("automationType", "Chronometer").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for Chronometer", ("property", property), ("available", "startTime, endTime, mode"));
        }

        private object ConfigureLever(Lever component, string property, string value, int buildingId, string name)
        {
            if (property == "springReturn")
            {
                if (!bool.TryParse(value, out var springReturn))
                    return _jw.Error("invalid_param: springReturn must be true or false", ("value", value));
                component.SetSpringReturn(springReturn);
                PostLog($"Configured Lever {buildingId} springReturn = {springReturn}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", springReturn).Prop("automationType", "Lever").CloseObj().ToString();
            }
            if (property == "pinned")
            {
                if (!bool.TryParse(value, out var pinned))
                    return _jw.Error("invalid_param: pinned must be true or false", ("value", value));
                component.SetPinned(pinned);
                PostLog($"Configured Lever {buildingId} pinned = {pinned}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", pinned).Prop("automationType", "Lever").CloseObj().ToString();
            }
            if (property == "state")
            {
                if (!bool.TryParse(value, out var state))
                    return _jw.Error("invalid_param: state must be true or false", ("value", value));
                component.SwitchState(state);
                PostLog($"Configured Lever {buildingId} state = {state}");
                return _jw.BeginObj().Prop("id", buildingId).Prop("name", name).Prop("property", property).Prop("value", state).Prop("automationType", "Lever").CloseObj().ToString();
            }
            return _jw.Error("invalid_param: unknown property for Lever", ("property", property), ("available", "springReturn, pinned, state"));
        }

        public object RenameEntity(int buildingId, string newName)
        {
            var ec = _cache.FindEntity(buildingId);
            if (ec == null)
                return _jw.Error("not_found: entity not found", ("id", buildingId));
            
            var named = ec.GetComponent<NamedEntity>();
            if (named == null)
                return _jw.Error("invalid_type: this entity cannot be renamed", ("id", buildingId), ("name", N(ec)));

            named.SetEntityName(newName);
            PostLog($"Renamed entity {buildingId} to '{newName}'");
            
            return _jw.BeginObj()
                .Prop("id", buildingId)
                .Prop("name", N(ec))
                .Prop("customName", named.EntityName)
                .CloseObj().ToString();
        }

        // PLACEMENT VALIDATION
    }
}

