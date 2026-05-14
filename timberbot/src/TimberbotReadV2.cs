// TimberbotReadV2.cs. All GET read endpoints. The core of the mod's read path.
//
// WHY THIS EXISTS
// ---------------
// Timberborn runs on Unity's main thread. Every frame the game updates beavers,
// buildings, water, weather, etc. We want external tools (AI agents, dashboards,
// scripts) to read this data over HTTP without stalling the game. The solution:
// snapshot the live game state on the main thread, then serve HTTP requests from
// the snapshot on a background thread.
//
// HOW IT WORKS (the snapshot pipeline)
// ------------------------------------
// 1. An HTTP GET arrives on the background listener thread.
// 2. The endpoint calls RequestFresh(), which sets a flag and blocks.
// 3. Next frame, ProcessPendingRefresh() runs on the main thread:
//    - Reads live game state (building workers, beaver needs, tree growth, etc.)
//    - Copies values into DTO buffers (BuildingState, BeaverState, etc.)
//    - Respects a per-frame time budget (~1ms) so the game stays smooth.
//    - Queues expensive finalize work (JSON pre-build) to a background thread.
// 4. The background finalize thread publishes the immutable snapshot.
// 5. All waiting HTTP readers wake up and serialize from the published snapshot.
//
// This means: reads never touch live game objects on the background thread,
// the game thread spends at most ~1ms per frame on our snapshots, and multiple
// concurrent HTTP readers share the same snapshot (no duplicate work).
//
// KEY ABSTRACTIONS
// ----------------
// ProjectionSnapshot<TDef, TState, TDetail>
//   Generic snapshot pipeline for entity collections (buildings, beavers, trees).
//   TDef = static data (id, name, coords) set once at entity creation.
//   TState = mutable data (workers, wellbeing) refreshed every snapshot.
//   TDetail = expensive data (inventory strings) only captured on detail requests.
//   Uses double-buffered capture arrays so the main thread writes while readers
//   read the previously published snapshot.
//
// ValueStore<TCapture, TSnapshot>
//   Same pattern for singleton endpoints (time, weather, speed, science).
//   Capture reads live state on main thread, finalize may do JSON pre-build
//   off-thread, publish makes it available to readers.
//
// CollectionRoute / ValueRoute / FlatArrayRoute
//   HTTP endpoint helpers that handle format (toon/json), pagination, filtering,
//   and the request-fresh-then-serialize flow. One route per endpoint.
//
// TrackedBuildingRef / TrackedBeaverRef / TrackedNaturalResourceRef
//   Live references to game entities, held so we can read their components each
//   frame. Added on EntityInitializedEvent, removed on EntityDeletedEvent.
//
// THREAD SAFETY RULES
// -------------------
// - Background thread reads ONLY from published snapshots or explicit thread-safe
//   services (IThreadSafeWaterMap, IThreadSafeColumnTerrainMap).
// - Background thread NEVER walks live entity/component graphs.
// - Main thread captures into DTO buffers, background finalizes and publishes.
// - The finalize thread is a single dedicated thread (not ThreadPool) so
//   publish order is deterministic.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading;
using System.Diagnostics;
using Timberborn.BlockSystem;
using Timberborn.BuilderPrioritySystem;
using Timberborn.Buildings;
using Timberborn.BuildingsReachability;
using Timberborn.Bots;
using Timberborn.Carrying;
using Timberborn.ConstructionSites;
using Timberborn.Cutting;
using Timberborn.DeteriorationSystem;
using Timberborn.DwellingSystem;
using Timberborn.EntitySystem;
using Timberborn.GameFactionSystem;
using Timberborn.GameCycleSystem;
using Timberborn.GameDistricts;
using Timberborn.Gathering;
using Timberborn.Forestry;
using Timberborn.InventorySystem;
using Timberborn.StockpilePrioritySystem;
using Timberborn.LifeSystem;
using Timberborn.MapIndexSystem;
using Timberborn.MechanicalSystem;
using Timberborn.NaturalResourcesLifecycle;
using Timberborn.NeedSystem;
using Timberborn.NeedSpecs;
using Timberborn.NotificationSystem;
using Timberborn.PowerManagement;
using Timberborn.PrioritySystem;
using Timberborn.RangedEffectSystem;
using Timberborn.ResourceCountingSystem;
using Timberborn.Reproduction;
using Timberborn.ScienceSystem;
using Timberborn.SingletonSystem;
using Timberborn.SoilContaminationSystem;
using Timberborn.SoilMoistureSystem;
using Timberborn.StatusSystem;
using Timberborn.TerrainSystem;
using Timberborn.TimeSystem;
using Timberborn.WaterBuildings;
using Timberborn.WaterSystem;
using Timberborn.WeatherSystem;
using Timberborn.Wellbeing;
using Timberborn.Wonders;
using Timberborn.WorkSystem;
using Timberborn.Workshops;
using Timberborn.Automation;
using Timberborn.AutomationBuildings;
using UnityEngine;

namespace Timberbot
{
    public class TimberbotReadV2
    {
        private readonly EntityRegistry _entityRegistry;
        private readonly EventBus _eventBus;
        private readonly GameCycleService _gameCycleService;
        private readonly WeatherService _weatherService;
        private readonly IDayNightCycle _dayNightCycle;
        private readonly SpeedManager _speedManager;
        private readonly WorkingHoursManager _workingHoursManager;
        private readonly TimberbotEntityRegistry _cache;
        private readonly ScienceService _scienceService;
        private readonly BuildingService _buildingService;
        private readonly BuildingUnlockingService _buildingUnlockingService;
        private readonly DistrictCenterRegistry _districtCenterRegistry;
        internal readonly FactionNeedService _factionNeedService;
        private readonly NotificationSaver _notificationSaver;
        private readonly ITerrainService _terrainService;
        private readonly IThreadSafeWaterMap _waterMap;
        private readonly MapIndexService _mapIndexService;
        private readonly IThreadSafeColumnTerrainMap _terrainMap;
        private readonly ISoilContaminationService _soilContaminationService;
        private readonly ISoilMoistureService _soilMoistureService;
        // --- Tracked entity refs ---
        // Live references to game entities. The main thread reads their components
        // each snapshot cycle. Added/removed via EventBus entity lifecycle events.
        private readonly List<TrackedBuildingRef> _tracked = new List<TrackedBuildingRef>();
        private readonly Dictionary<Guid, TrackedBuildingRef> _trackedById = new Dictionary<Guid, TrackedBuildingRef>();
        private readonly List<TrackedBeaverRef> _trackedBeavers = new List<TrackedBeaverRef>();
        private readonly Dictionary<Guid, TrackedBeaverRef> _trackedBeaversById = new Dictionary<Guid, TrackedBeaverRef>();
        private readonly List<TrackedNaturalResourceRef> _trackedNaturalResources = new List<TrackedNaturalResourceRef>();
        private readonly Dictionary<Guid, TrackedNaturalResourceRef> _trackedNaturalResourcesById = new Dictionary<Guid, TrackedNaturalResourceRef>();
        private readonly List<TrackedBlockerRef> _trackedBlockers = new List<TrackedBlockerRef>();
        private readonly Dictionary<Guid, TrackedBlockerRef> _trackedBlockersById = new Dictionary<Guid, TrackedBlockerRef>();
        // --- Snapshot infrastructure ---
        // Each ProjectionSnapshot holds the capture buffers and published snapshot
        // for one entity type. ValueStores do the same for singleton endpoints.
        private readonly TimberbotJw _jw = new TimberbotJw(300000);
        private readonly TimberbotJw _configJw = new TimberbotJw(1024);
        private readonly ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState> _snapshot;
        private readonly ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState> _beaverSnapshot;
        private readonly ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail> _naturalResourceSnapshot;
        private readonly CollectionRoute<BuildingDefinition, BuildingState, BuildingDetailState> _buildingsEndpoint;
        private readonly CollectionRoute<BeaverDefinition, BeaverState, BeaverDetailState> _beaversEndpoint;
        private readonly CollectionRoute<NaturalResourceDefinition, NaturalResourceState, NoDetail> _treesEndpoint;
        private readonly CollectionRoute<NaturalResourceDefinition, NaturalResourceState, NoDetail> _cropsEndpoint;
        private readonly CollectionRoute<NaturalResourceDefinition, NaturalResourceState, NoDetail> _gatherablesEndpoint;
        private readonly ValueStore<SettlementSnapshot, SettlementSnapshot> _settlementStore = new ValueStore<SettlementSnapshot, SettlementSnapshot>();
        private readonly ValueStore<TimeSnapshot, TimeSnapshot> _timeStore = new ValueStore<TimeSnapshot, TimeSnapshot>();
        private readonly ValueStore<WeatherSnapshot, WeatherSnapshot> _weatherStore = new ValueStore<WeatherSnapshot, WeatherSnapshot>();
        private readonly ValueStore<SpeedSnapshot, SpeedSnapshot> _speedStore = new ValueStore<SpeedSnapshot, SpeedSnapshot>();
        private readonly ValueStore<WorkHoursSnapshot, WorkHoursSnapshot> _workHoursStore = new ValueStore<WorkHoursSnapshot, WorkHoursSnapshot>();
        private readonly ValueStore<ScienceCapture, RawJsonSnapshot> _scienceStore = new ValueStore<ScienceCapture, RawJsonSnapshot>();
        private readonly ValueStore<DistributionCapture, RawJsonSnapshot> _distributionStore = new ValueStore<DistributionCapture, RawJsonSnapshot>();
        private readonly ValueStore<NotificationItem[], NotificationItem[]> _notificationsStore = new ValueStore<NotificationItem[], NotificationItem[]>();
        private readonly ValueStore<DistrictCapture[], DistrictSnapshot[]> _districtStore = new ValueStore<DistrictCapture[], DistrictSnapshot[]>();
        private readonly ValueRoute<TimeSnapshot> _timeRoute;
        private readonly ValueRoute<WeatherSnapshot> _weatherRoute;
        private readonly ValueRoute<SpeedSnapshot> _speedRoute;
        private readonly ValueRoute<WorkHoursSnapshot> _workHoursRoute;
        private readonly ValueRoute<RawJsonSnapshot> _scienceRoute;
        private readonly ValueRoute<RawJsonSnapshot> _distributionRoute;
        private readonly FlatArrayRoute<NotificationItem> _notificationsRoute;
        private readonly FlatArrayRoute<AlertItem> _alertsRoute;
        private readonly ValueRoute<PowerNetworkItem[]> _powerRoute;
        private readonly Dictionary<string, int[]> _treeSpecies = new Dictionary<string, int[]>();
        private readonly Dictionary<string, int[]> _cropSpecies = new Dictionary<string, int[]>();
        private readonly Dictionary<string, int> _roleCounts = new Dictionary<string, int>();
        private readonly Dictionary<string, int[]> _districtStats = new Dictionary<string, int[]>();
        private readonly Dictionary<string, int> _alertCounts = new Dictionary<string, int>();
        private readonly Dictionary<string, (int x, int y, int z, string orientation, int entranceX, int entranceY)> _districtDCs = new Dictionary<string, (int, int, int, string, int, int)>();
        private readonly Dictionary<string, string> _needToGroup = new Dictionary<string, string>();
        private readonly Dictionary<string, float> _groupMaxPerBeaver = new Dictionary<string, float>();
        private readonly Dictionary<string, float> _wbGroupTotals = new Dictionary<string, float>();
        private readonly Dictionary<string, List<NeedSpec>> _wbGroupNeeds = new Dictionary<string, List<NeedSpec>>();
        private readonly Dictionary<string, float> _wbGroupMaxTotals = new Dictionary<string, float>();
        private readonly Dictionary<string, float[]> _districtWb = new Dictionary<string, float[]>();
        private NeedSpec[] _cachedBeaverNeeds;
        private readonly Dictionary<string, int> _resourceTotals = new Dictionary<string, int>();
        private readonly Dictionary<long, int[]> _clusterCells = new Dictionary<long, int[]>();
        private readonly Dictionary<long, Dictionary<string, int>> _clusterSpecies = new Dictionary<long, Dictionary<string, int>>();
        private readonly List<long> _clusterSorted = new List<long>();
        private readonly Dictionary<long, List<(string name, int z)>> _tileOccupants = new Dictionary<long, List<(string, int)>>();
        private readonly HashSet<long> _tileEntrances = new HashSet<long>();
        private readonly HashSet<long> _tileSeedlings = new HashSet<long>();
        private readonly HashSet<long> _tileDeadTiles = new HashSet<long>();
        private readonly StringBuilder _tileSb = new StringBuilder(256);
        // --- Background finalize thread ---
        // Expensive work (JSON pre-build for science/distribution, building detail
        // string assembly) runs here so the main thread stays under budget.
        private readonly object _finalizeQueueLock = new object();
        private readonly Queue<Action> _finalizeQueue = new Queue<Action>();
        private readonly AutoResetEvent _finalizeSignal = new AutoResetEvent(false);
        private readonly ManualResetEvent _finalizeStop = new ManualResetEvent(false);
        private readonly Thread _finalizeThread;
        private const double CaptureBudgetMs = 1.0;
        private static readonly HashSet<string> _cropNames = new HashSet<string>
            { "Kohlrabi", "Soybean", "Corn", "Sunflower", "Eggplant", "Algae", "Cassava", "Mushroom", "Potato", "Wheat", "Carrot" };
        public static readonly int[] SpeedScale = { 0, 1, 3, 7 };
        // Building role classification for the summary endpoint. Maps building names
        // to categories (water, food, housing, etc.) so the AI gets role counts.
        private static readonly Dictionary<string, string[]> _roleMap = new Dictionary<string, string[]> {
            {"water", new[]{"Pump","Tank","FluidDump"}},
            {"aquifer", new[]{"AquiferDrill"}},
            {"food", new[]{"FarmHouse","AquaticFarmhouse","EfficientFarmHouse","Gatherer","Grill","Gristmill","Bakery","FoodFactory","Fermenter","HydroponicGarden"}},
            {"housing", new[]{"Lodge","MiniLodge","DoubleLodge","TripleLodge","Rowhouse","Barrack"}},
            {"wood", new[]{"Lumberjack","LumberMill","IndustrialLumberMill","Forester"}},
            {"storage", new[]{"Warehouse","Pile","ReservePile","ReserveWarehouse","ReserveTank"}},
            {"power", new[]{"PowerWheel","LargePowerWheel","WaterWheel","WindTurbine","LargeWindTurbine","SteamEngine","PowerShaft","Clutch","GravityBattery"}},
            {"science", new[]{"Inventor","Numbercruncher","Observatory"}},
            {"production", new[]{"GearWorkshop","Smelter","Metalsmith","Scavenger","Mine","BotAssembler","BotPartFactory","PaperMill","PrintingPress","WoodWorkshop","Centrifuge","ExplosivesFactory","Refinery"}},
            {"leisure", new[]{"Campfire","Scratcher","Shower","DoubleShower","SwimmingPool","Carousel","MudPit","MudBath","ExercisePlaza","WindTunnel","Motivatorium","Lido","Detailer","ContemplationSpot","Agora","DanceHall","RooftopTerrace","MedicalBed","TeethGrindstone","Herbalist","DecontaminationPod","ChargingStation"}},
        };

        public TimberbotReadV2(
            EntityRegistry entityRegistry,
            EventBus eventBus,
            GameCycleService gameCycleService,
            WeatherService weatherService,
            IDayNightCycle dayNightCycle,
            SpeedManager speedManager,
            WorkingHoursManager workingHoursManager,
            TimberbotEntityRegistry cache,
            ITerrainService terrainService,
            IThreadSafeWaterMap waterMap,
            MapIndexService mapIndexService,
            IThreadSafeColumnTerrainMap terrainMap,
            ISoilContaminationService soilContaminationService,
            ISoilMoistureService soilMoistureService,
            ScienceService scienceService,
            BuildingService buildingService,
            BuildingUnlockingService buildingUnlockingService,
            DistrictCenterRegistry districtCenterRegistry,
            FactionNeedService factionNeedService,
            NotificationSaver notificationSaver)
        {
            _entityRegistry = entityRegistry;
            _eventBus = eventBus;
            _gameCycleService = gameCycleService;
            _weatherService = weatherService;
            _dayNightCycle = dayNightCycle;
            _speedManager = speedManager;
            _workingHoursManager = workingHoursManager;
            _cache = cache;
            _terrainService = terrainService;
            _waterMap = waterMap;
            _mapIndexService = mapIndexService;
            _terrainMap = terrainMap;
            _soilContaminationService = soilContaminationService;
            _soilMoistureService = soilMoistureService;
            _scienceService = scienceService;
            _buildingService = buildingService;
            _buildingUnlockingService = buildingUnlockingService;
            _districtCenterRegistry = districtCenterRegistry;
            _factionNeedService = factionNeedService;
            _notificationSaver = notificationSaver;

            _snapshot = new ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>(new BuildingCollectionSchema());
            _beaverSnapshot = new ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState>(new BeaverCollectionSchema());
            _naturalResourceSnapshot = new ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>(new TreeCollectionSchema());

            _buildingsEndpoint = new CollectionRoute<BuildingDefinition, BuildingState, BuildingDetailState>(
                _jw,
                (fullDetail, timeoutMs) => _snapshot.RequestFresh(fullDetail, timeoutMs),
                new BuildingCollectionSchema());
            _beaversEndpoint = new CollectionRoute<BeaverDefinition, BeaverState, BeaverDetailState>(
                _jw,
                (fullDetail, timeoutMs) => _beaverSnapshot.RequestFresh(fullDetail, timeoutMs),
                new BeaverCollectionSchema());
            _treesEndpoint = new CollectionRoute<NaturalResourceDefinition, NaturalResourceState, NoDetail>(
                _jw,
                (fullDetail, timeoutMs) => _naturalResourceSnapshot.RequestFresh(false, timeoutMs),
                new TreeCollectionSchema());
            _cropsEndpoint = new CollectionRoute<NaturalResourceDefinition, NaturalResourceState, NoDetail>(
                _jw,
                (fullDetail, timeoutMs) => _naturalResourceSnapshot.RequestFresh(false, timeoutMs),
                new CropCollectionSchema());
            _gatherablesEndpoint = new CollectionRoute<NaturalResourceDefinition, NaturalResourceState, NoDetail>(
                _jw,
                (fullDetail, timeoutMs) => _naturalResourceSnapshot.RequestFresh(false, timeoutMs),
                new GatherableCollectionSchema());
            _timeRoute = new ValueRoute<TimeSnapshot>(
                new TimberbotJw(256),
                timeoutMs => _timeStore.RequestFresh(timeoutMs),
                new TimeSchema());
            _weatherRoute = new ValueRoute<WeatherSnapshot>(
                new TimberbotJw(256),
                timeoutMs => _weatherStore.RequestFresh(timeoutMs),
                new WeatherSchema());
            _speedRoute = new ValueRoute<SpeedSnapshot>(
                new TimberbotJw(64),
                timeoutMs => _speedStore.RequestFresh(timeoutMs),
                new SpeedSchema());
            _workHoursRoute = new ValueRoute<WorkHoursSnapshot>(
                new TimberbotJw(96),
                timeoutMs => _workHoursStore.RequestFresh(timeoutMs),
                new WorkHoursSchema());
            _scienceRoute = new ValueRoute<RawJsonSnapshot>(
                new TimberbotJw(8192),
                timeoutMs => _scienceStore.RequestFresh(timeoutMs),
                new RawJsonSchema());
            _distributionRoute = new ValueRoute<RawJsonSnapshot>(
                new TimberbotJw(8192),
                timeoutMs => _distributionStore.RequestFresh(timeoutMs),
                new RawJsonSchema());
            _notificationsRoute = new FlatArrayRoute<NotificationItem>(
                new TimberbotJw(8192),
                timeoutMs => _notificationsStore.RequestFresh(timeoutMs),
                new NotificationSchema());
            _alertsRoute = new FlatArrayRoute<AlertItem>(
                new TimberbotJw(8192),
                _ => BuildAlertsFromBuildings(),
                new AlertSchema());
            _powerRoute = new ValueRoute<PowerNetworkItem[]>(
                new TimberbotJw(8192),
                _ => BuildPowerFromBuildings(),
                new PowerSchema());
            _finalizeThread = new Thread(FinalizeLoop)
            {
                IsBackground = true,
                Name = "TimberbotReadV2Finalize"
            };
            _finalizeThread.Start();
        }

        public int PublishSequence => _snapshot.Sequence;
        public int LastPublishedCount => _snapshot.Count;
        public float LastPublishedAt => _snapshot.PublishedAt;
        public double LastCaptureMs => _snapshot.LastCaptureMs;
        public double LastFinalizeMs => _snapshot.LastFinalizeMs;
        public int PendingWaiters => _snapshot.PendingWaiterCount;
        public int RefreshInFlight => _snapshot.InFlight ? 1 : 0;
        public int BeaverPublishSequence => _beaverSnapshot.Sequence;
        public int BeaverLastPublishedCount => _beaverSnapshot.Count;
        public float BeaverLastPublishedAt => _beaverSnapshot.PublishedAt;
        public double BeaverLastCaptureMs => _beaverSnapshot.LastCaptureMs;
        public double BeaverLastFinalizeMs => _beaverSnapshot.LastFinalizeMs;
        public int BeaverPendingWaiters => _beaverSnapshot.PendingWaiterCount;
        public int BeaverRefreshInFlight => _beaverSnapshot.InFlight ? 1 : 0;
        public int NaturalResourcePublishSequence => _naturalResourceSnapshot.Sequence;
        public int NaturalResourceLastPublishedCount => _naturalResourceSnapshot.Count;
        public float NaturalResourceLastPublishedAt => _naturalResourceSnapshot.PublishedAt;
        public double NaturalResourceLastCaptureMs => _naturalResourceSnapshot.LastCaptureMs;
        public double NaturalResourceLastFinalizeMs => _naturalResourceSnapshot.LastFinalizeMs;
        public int NaturalResourcePendingWaiters => _naturalResourceSnapshot.PendingWaiterCount;
        public int NaturalResourceRefreshInFlight => _naturalResourceSnapshot.InFlight ? 1 : 0;
        public int DistrictPublishSequence => _districtStore.Sequence;
        public int DistrictLastPublishedCount => _districtStore.Count;
        public float DistrictLastPublishedAt => _districtStore.PublishedAt;
        public double DistrictLastCaptureMs => _districtStore.LastCaptureMs;
        public double DistrictLastFinalizeMs => _districtStore.LastFinalizeMs;
        public int DistrictPendingWaiters => _districtStore.PendingWaiterCount;
        public int DistrictRefreshInFlight => _districtStore.InFlight ? 1 : 0;
        internal ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>.Snapshot CurrentBuildingSnapshot => _snapshot.Current;
        internal ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState>.Snapshot CurrentBeaverSnapshot => _beaverSnapshot.Current;
        internal ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot CurrentNaturalResourceSnapshot => _naturalResourceSnapshot.Current;
        internal DistrictSnapshot[] CurrentDistrictSnapshot => _districtStore.Current;
        internal ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>.Snapshot Buildings => _snapshot.Current;
        internal ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState>.Snapshot Beavers => _beaverSnapshot.Current;
        internal ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot NaturalResources => _naturalResourceSnapshot.Current;
        internal DistrictSnapshot[] Districts => _districtStore.Current;
        internal IReadOnlyList<TrackedBuildingRef> TrackedBuildings => _tracked;
        internal IReadOnlyList<TrackedBeaverRef> TrackedBeavers => _trackedBeavers;
        internal IReadOnlyList<TrackedNaturalResourceRef> TrackedNaturalResources => _trackedNaturalResources;
        internal IReadOnlyList<TrackedBlockerRef> TrackedBlockers => _trackedBlockers;
        internal DistrictCenterRegistry DebugDistrictRegistry => _districtCenterRegistry;

        private void EnqueueFinalize(Action action)
        {
            lock (_finalizeQueueLock)
                _finalizeQueue.Enqueue(action);
            _finalizeSignal.Set();
        }

        private void FinalizeLoop()
        {
            var waitHandles = new WaitHandle[] { _finalizeStop, _finalizeSignal };
            while (true)
            {
                Action action = null;
                lock (_finalizeQueueLock)
                {
                    if (_finalizeQueue.Count > 0)
                        action = _finalizeQueue.Dequeue();
                }

                if (action == null)
                {
                    int index = WaitHandle.WaitAny(waitHandles);
                    if (index == 0)
                    {
                        lock (_finalizeQueueLock)
                        {
                            if (_finalizeQueue.Count == 0)
                                return;
                        }
                    }
                    continue;
                }

                try
                {
                    action();
                }
                catch (Exception ex)
                {
                    TimberbotLog.Error("readv2.finalize", ex);
                }
            }
        }

        public void Register() => _eventBus.Register(this);
        public void Unregister()
        {
            _eventBus.Unregister(this);
            _finalizeStop.Set();
            _finalizeSignal.Set();
            if (_finalizeThread.IsAlive)
                _finalizeThread.Join(2000);
        }

        // Called once when the game loads. Walks every entity in the game,
        // registers tracked refs, and publishes the first snapshot synchronously
        // so the API is ready before the first HTTP request arrives.
        public void BuildAll()
        {
            _tracked.Clear();
            _trackedById.Clear();
            _trackedBeavers.Clear();
            _trackedBeaversById.Clear();
            _trackedNaturalResources.Clear();
            _trackedNaturalResourcesById.Clear();
            _trackedBlockers.Clear();
            _trackedBlockersById.Clear();
            foreach (var ec in _entityRegistry.Entities)
            {
                TryAddTrackedBuilding(ec);
                TryAddTrackedBeaver(ec);
                TryAddTrackedNaturalResource(ec);
                TryAddTrackedBlocker(ec);
            }
            _snapshot.MarkDirty();
            _beaverSnapshot.MarkDirty();
            _naturalResourceSnapshot.MarkDirty();
            _snapshot.PublishNow(0f, _tracked.Count,
                i => _tracked[i].Definition,
                (s, i) => RefreshState(s, _tracked[i]),
                null,
                FinalizeBuildingSnapshot);
            _beaverSnapshot.PublishNow(0f, _trackedBeavers.Count,
                i => _trackedBeavers[i].Definition,
                (s, i) => RefreshState(s, _trackedBeavers[i]));
            _naturalResourceSnapshot.PublishNow(0f, _trackedNaturalResources.Count,
                i => _trackedNaturalResources[i].Definition,
                (s, i) => RefreshState(s, _trackedNaturalResources[i]));
            _settlementStore.PublishNow(0f, BuildSettlementSnapshot, IdentitySnapshot);
            _timeStore.PublishNow(0f, BuildTimeSnapshot, IdentitySnapshot);
            _weatherStore.PublishNow(0f, BuildWeatherSnapshot, IdentitySnapshot);
            _speedStore.PublishNow(0f, BuildSpeedSnapshot, IdentitySnapshot);
            _workHoursStore.PublishNow(0f, BuildWorkHoursSnapshot, IdentitySnapshot);
            _scienceStore.PublishNow(0f, CaptureScienceSnapshot, FinalizeScienceSnapshot);
            _distributionStore.PublishNow(0f, CaptureDistributionSnapshot, FinalizeDistributionSnapshot);
            _notificationsStore.PublishNow(0f, BuildNotificationsSnapshot, IdentitySnapshot);
            _districtStore.PublishNow(0f, CaptureDistrictSnapshots, FinalizeDistrictSnapshots);
            _cachedBeaverNeeds = _factionNeedService.GetBeaverNeeds().ToArray();
        }

        public void InvalidateBuildings() => _snapshot.Invalidate();
        public void InvalidateBeavers() => _beaverSnapshot.Invalidate();
        public void InvalidateNaturalResources() => _naturalResourceSnapshot.Invalidate();

        // Called every frame from UpdateSingleton(). Checks if any snapshot has
        // waiting readers, and if so, captures live state into DTO buffers.
        // Respects a time budget (~1ms) so the game stays smooth. if budget is
        // exceeded mid-capture, it resumes next frame where it left off.
        // After capture completes, expensive finalize work is queued to the
        // background finalize thread, which publishes the snapshot and wakes readers.
        public void ProcessPendingRefresh(float now)
        {
            var budget = Stopwatch.StartNew();
            _snapshot.ProcessPendingCapture(now, _tracked.Count,
                i => _tracked[i].Definition,
                (s, i) => RefreshState(s, _tracked[i]),
                (d, i) => RefreshDetail(d, _tracked[i]),
                FinalizeBuildingSnapshot,
                EnqueueFinalize,
                () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _beaverSnapshot.ProcessPendingCapture(now, _trackedBeavers.Count,
                i => _trackedBeavers[i].Definition,
                (s, i) => RefreshState(s, _trackedBeavers[i]),
                (d, i) => RefreshDetail(d, _trackedBeavers[i]),
                null,
                EnqueueFinalize,
                () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _naturalResourceSnapshot.ProcessPendingCapture(now, _trackedNaturalResources.Count,
                i => _trackedNaturalResources[i].Definition,
                (s, i) => RefreshState(s, _trackedNaturalResources[i]),
                null,
                null,
                EnqueueFinalize,
                () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _settlementStore.ProcessPendingCapture(now, BuildSettlementSnapshot, IdentitySnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _timeStore.ProcessPendingCapture(now, BuildTimeSnapshot, IdentitySnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _weatherStore.ProcessPendingCapture(now, BuildWeatherSnapshot, IdentitySnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _speedStore.ProcessPendingCapture(now, BuildSpeedSnapshot, IdentitySnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _workHoursStore.ProcessPendingCapture(now, BuildWorkHoursSnapshot, IdentitySnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _scienceStore.ProcessPendingCapture(now, CaptureScienceSnapshot, FinalizeScienceSnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _distributionStore.ProcessPendingCapture(now, CaptureDistributionSnapshot, FinalizeDistributionSnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _notificationsStore.ProcessPendingCapture(now, BuildNotificationsSnapshot, IdentitySnapshot, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
            if (budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs) return;
            _districtStore.ProcessPendingCapture(now, CaptureDistrictSnapshots, FinalizeDistrictSnapshots, EnqueueFinalize, () => budget.Elapsed.TotalMilliseconds >= CaptureBudgetMs);
        }

        internal ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>.Snapshot EnsureBuildingsFreshNow(float now, bool fullDetail = false)
            => _snapshot.PublishNow(now, _tracked.Count, i => _tracked[i].Definition, (s, i) => RefreshState(s, _tracked[i]), fullDetail ? (d, i) => RefreshDetail(d, _tracked[i]) : null, FinalizeBuildingSnapshot);

        internal ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot EnsureNaturalResourcesFreshNow(float now)
            => _naturalResourceSnapshot.PublishNow(now, _trackedNaturalResources.Count, i => _trackedNaturalResources[i].Definition, (s, i) => RefreshState(s, _trackedNaturalResources[i]));

        internal ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState>.Snapshot EnsureBeaversFreshNow(float now, bool fullDetail = false)
            => _beaverSnapshot.PublishNow(now, _trackedBeavers.Count, i => _trackedBeavers[i].Definition, (s, i) => RefreshState(s, _trackedBeavers[i]), fullDetail ? (d, i) => RefreshDetail(d, _trackedBeavers[i]) : null);

        internal DistrictSnapshot[] EnsureDistrictsFreshNow(float now)
            => _districtStore.PublishNow(now, CaptureDistrictSnapshots, FinalizeDistrictSnapshots);

        public object CollectBuildings(string format = "toon", string detail = "basic", int id = 0, int limit = 100, int offset = 0,
            string filterName = null, int filterX = 0, int filterY = 0, int filterRadius = 0)
            => _buildingsEndpoint.Collect(format, detail, id, limit, offset, filterName, filterX, filterY, filterRadius);

        // CollectSummary: the single-call colony overview.
        // Returns everything an AI needs to assess the colony: population, resources,
        // weather, drought countdown, tree/food clusters, building roles, wellbeing,
        // and projected days of critical supplies. Aggregates across all districts.
        public object CollectSummary(string format = "toon")
        {
            ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot naturalSnapshot;
            ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>.Snapshot buildingSnapshot;
            ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState>.Snapshot beaverSnapshot;
            DistrictSnapshot[] districts;
            try
            {
                naturalSnapshot = _naturalResourceSnapshot.RequestFresh(false, 2000);
                buildingSnapshot = _snapshot.RequestFresh(false, 2000);
                beaverSnapshot = _beaverSnapshot.RequestFresh(true, 2000);
                districts = _districtStore.RequestFresh(2000);
            }
            catch (TimeoutException)
            {
                return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s");
            }

            int treeMarkedGrown = 0, treeMarkedSeedling = 0, treeUnmarkedGrown = 0;
            int cropReady = 0, cropGrowing = 0;
            _treeSpecies.Clear();
            _cropSpecies.Clear();
            var treeSpecies = _treeSpecies;
            var cropSpecies = _cropSpecies;
            int occupiedBeds = 0, totalBeds = 0;
            int totalVacancies = 0, assignedWorkers = 0;
            float totalWellbeing = 0f;
            int beaverCount = 0;
            int organicBeaverCount = 0;
            _alertCounts.Clear();
            var alertCounts = _alertCounts;
            int miserable = 0, critical = 0;

            for (int i = 0; i < naturalSnapshot.Count; i++)
            {
                var d = naturalSnapshot.Definitions[i];
                var c = naturalSnapshot.States[i];
                if (d.IsTree == 0 && d.IsCrop == 0) continue;
                if (c.Alive == 0) continue;
                if (d.IsCrop != 0)
                {
                    if (c.Grown != 0) cropReady++; else cropGrowing++;
                    if (!cropSpecies.TryGetValue(d.Name, out var cs)) { cs = new int[2]; cropSpecies[d.Name] = cs; }
                    if (c.Grown != 0) cs[0]++; else cs[1]++;
                }
                else
                {
                    if (c.Marked != 0 && c.Grown != 0) treeMarkedGrown++;
                    else if (c.Marked != 0 && c.Grown == 0) treeMarkedSeedling++;
                    else if (c.Marked == 0 && c.Grown != 0) treeUnmarkedGrown++;
                    if (!treeSpecies.TryGetValue(d.Name, out var ts)) { ts = new int[3]; treeSpecies[d.Name] = ts; }
                    if (c.Marked != 0 && c.Grown != 0) ts[0]++;
                    else if (c.Marked == 0 && c.Grown != 0) ts[1]++;
                    else if (c.Marked != 0 && c.Grown == 0) ts[2]++;
                }
            }

            int dcX = 0, dcY = 0, dcZ = 0;
            bool foundDC = false;
            _roleCounts.Clear();
            _districtStats.Clear();
            _districtDCs.Clear();
            var roleCounts = _roleCounts;
            var districtStats = _districtStats;
            var districtDCs = _districtDCs;
            var roleMap = _roleMap;

            for (int i = 0; i < buildingSnapshot.Count; i++)
            {
                var d = buildingSnapshot.Definitions[i];
                var c = buildingSnapshot.States[i];
                var dname = c.District ?? "_unknown";
                if (!districtStats.TryGetValue(dname, out var ds)) { ds = new int[4]; districtStats[dname] = ds; }
                occupiedBeds += c.Dwellers;
                totalBeds += c.MaxDwellers;
                ds[0] += c.Dwellers;
                ds[1] += c.MaxDwellers;
                assignedWorkers += c.AssignedWorkers;
                totalVacancies += c.DesiredWorkers;
                ds[2] += c.AssignedWorkers;
                ds[3] += c.DesiredWorkers;
                if (c.StatusAlerts != null && c.StatusAlerts.Length > 0)
                {
                    for (int ai = 0; ai < c.StatusAlerts.Length; ai++)
                        alertCounts[c.StatusAlerts[ai]] = alertCounts.GetValueOrDefault(c.StatusAlerts[ai]) + 1;
                }
                if (d.Name != null && d.Name.Contains("DistrictCenter"))
                {
                    var dcOri = d.Orientation ?? "south";
                    int eX = d.X + 1, eY = d.Y + 1;
                    if (dcOri == "south") { eX = d.X + 1; eY = d.Y - 1; }
                    else if (dcOri == "north") { eX = d.X + 1; eY = d.Y + 3; }
                    else if (dcOri == "east") { eX = d.X + 3; eY = d.Y + 1; }
                    else if (dcOri == "west") { eX = d.X - 1; eY = d.Y + 1; }
                    districtDCs[dname] = (d.X, d.Y, d.Z, dcOri, eX, eY);
                    if (!foundDC) { foundDC = true; dcX = d.X; dcY = d.Y; dcZ = d.Z; }
                }
                string name = d.Name ?? "";
                if (name == "Path") { roleCounts["paths"] = roleCounts.GetValueOrDefault("paths") + 1; continue; }
                bool matched = false;
                foreach (var kv in roleMap)
                {
                    foreach (var keyword in kv.Value)
                    {
                        if (name.Contains(keyword)) { roleCounts[kv.Key] = roleCounts.GetValueOrDefault(kv.Key) + 1; matched = true; break; }
                    }
                    if (matched) break;
                }
                if (!matched) roleCounts["other"] = roleCounts.GetValueOrDefault("other") + 1;
            }

            string faction = TimberbotEntityRegistry.FactionSuffix == ".Folktails" ? "Folktails" : TimberbotEntityRegistry.FactionSuffix == ".IronTeeth" ? "IronTeeth" : "unknown";
            var beaverNeeds = _cachedBeaverNeeds;
            _needToGroup.Clear();
            _groupMaxPerBeaver.Clear();
            var needToGroup = _needToGroup;
            var groupMaxPerBeaver = _groupMaxPerBeaver;
            foreach (var ns in beaverNeeds)
            {
                if (string.IsNullOrEmpty(ns.NeedGroupId)) continue;
                needToGroup[ns.Id] = ns.NeedGroupId;
                groupMaxPerBeaver[ns.NeedGroupId] = groupMaxPerBeaver.GetValueOrDefault(ns.NeedGroupId) + ns.FavorableWellbeing;
            }
            _wbGroupTotals.Clear();
            _districtWb.Clear();
            var wbGroupTotals = _wbGroupTotals;
            var districtWb = _districtWb;
            for (int i = 0; i < beaverSnapshot.Count; i++)
            {
                var d = beaverSnapshot.Definitions[i];
                var c = beaverSnapshot.States[i];
                var detailState = beaverSnapshot.Details?[i];
                beaverCount++;
                if (d.IsBot == 0) {
                    totalWellbeing += c.Wellbeing;
                    organicBeaverCount++;
                    if (c.Wellbeing < 4) miserable++;
                    if (c.AnyCritical != 0) critical++;
                }
                var bDist = c.District ?? "_unknown";
                if (!districtWb.TryGetValue(bDist, out var dw)) { dw = new float[4]; districtWb[bDist] = dw; }
                if (d.IsBot == 0) {
                    dw[0] += c.Wellbeing;
                    dw[1]++;
                    if (c.Wellbeing < 4) dw[2]++;
                    if (c.AnyCritical != 0) dw[3]++;
                }
                if (d.IsBot == 0 && detailState?.Needs != null)
                    foreach (var n in detailState.Needs)
                        if (needToGroup.ContainsKey(n.Id))
                            wbGroupTotals[needToGroup[n.Id]] = wbGroupTotals.GetValueOrDefault(needToGroup[n.Id]) + n.Wellbeing;
            }

            int totalAdults = 0, totalChildren = 0, totalBots = 0;
            foreach (var dc in districts)
            { totalAdults += dc.Adults; totalChildren += dc.Children; totalBots += dc.Bots; }
            int organicTotal = organicBeaverCount;
            int homeless = Math.Max(0, organicTotal - occupiedBeds);
            int unemployed = Math.Max(0, totalAdults - assignedWorkers);
            float avgWellbeing = organicBeaverCount > 0 ? totalWellbeing / organicBeaverCount : 0;
            int currentSpeed = Array.IndexOf(SpeedScale, (int)_speedManager.CurrentSpeed);
            if (currentSpeed < 0) currentSpeed = 0;

            if (format == "json")
            {
                var jj = _jw.Reset().BeginObj();
                jj.Prop("settlement", GetSettlementName());
                jj.Prop("faction", faction);
                jj.Obj("time").Prop("dayNumber", _dayNightCycle.DayNumber).Prop("dayProgress", (float)_dayNightCycle.DayProgress).Prop("partialDayNumber", (float)_dayNightCycle.PartialDayNumber).Prop("speed", currentSpeed).CloseObj();
                jj.Obj("weather").Prop("cycle", (int)_gameCycleService.Cycle).Prop("cycleDay", _gameCycleService.CycleDay).Prop("isHazardous", _weatherService.IsHazardousWeather).Prop("temperateWeatherDuration", _weatherService.TemperateWeatherDuration).Prop("hazardousWeatherDuration", _weatherService.HazardousWeatherDuration).Prop("cycleLengthInDays", _weatherService.CycleLengthInDays).CloseObj();
                jj.Arr("districts");
                foreach (var dc in districts)
                {
                    jj.OpenObj().Prop("name", dc.Name);
                    jj.Obj("population").Prop("adults", dc.Adults).Prop("children", dc.Children).Prop("bots", dc.Bots).CloseObj();
                    jj.Obj("resources");
                    if (dc.Resources != null)
                        foreach (var kvp in dc.Resources) jj.Prop(kvp.Key, kvp.Value.all);
                    jj.CloseObj();
                    var ds = districtStats.GetValueOrDefault(dc.Name);
                    int dBeds = ds != null ? ds[1] : 0;
                    int dOccBeds = ds != null ? ds[0] : 0;
                    int dPop = dc.Adults + dc.Children + dc.Bots;
                    jj.Obj("housing").Prop("occupiedBeds", dOccBeds).Prop("totalBeds", dBeds).Prop("homeless", Math.Max(0, dPop - dOccBeds)).CloseObj();
                    int dAssigned = ds != null ? ds[2] : 0;
                    int dVacancies = ds != null ? ds[3] : 0;
                    jj.Obj("employment").Prop("assigned", dAssigned).Prop("vacancies", dVacancies).Prop("unemployed", Math.Max(0, dc.Adults - dAssigned)).CloseObj();
                    var dwb = districtWb.GetValueOrDefault(dc.Name);
                    float dAvgWb = dwb != null && dwb[1] > 0 ? dwb[0] / dwb[1] : 0;
                    jj.Obj("wellbeing").Prop("average", (float)Math.Round(dAvgWb, 1), "F1").Prop("miserable", (int)(dwb != null ? dwb[2] : 0)).Prop("critical", (int)(dwb != null ? dwb[3] : 0)).CloseObj();
                    if (districtDCs.TryGetValue(dc.Name, out var ddc))
                        jj.Obj("dc").Prop("x", ddc.x).Prop("y", ddc.y).Prop("z", ddc.z).Prop("orientation", ddc.orientation).Prop("entranceX", ddc.entranceX).Prop("entranceY", ddc.entranceY).CloseObj();
                    jj.CloseObj();
                }
                jj.CloseArr();
                jj.Obj("trees").Prop("markedGrown", treeMarkedGrown).Prop("markedSeedling", treeMarkedSeedling).Prop("unmarkedGrown", treeUnmarkedGrown);
                jj.Arr("species");
                foreach (var kv in treeSpecies)
                    jj.OpenObj().Prop("name", kv.Key).Prop("markedGrown", kv.Value[0]).Prop("unmarkedGrown", kv.Value[1]).Prop("seedling", kv.Value[2]).CloseObj();
                jj.CloseArr().CloseObj();
                jj.Obj("crops").Prop("ready", cropReady).Prop("growing", cropGrowing);
                jj.Arr("species");
                foreach (var kv in cropSpecies)
                    jj.OpenObj().Prop("name", kv.Key).Prop("ready", kv.Value[0]).Prop("growing", kv.Value[1]).CloseObj();
                jj.CloseArr().CloseObj();
                jj.Obj("wellbeing").Prop("average", avgWellbeing, "F1").Prop("miserable", miserable).Prop("critical", critical);
                jj.Arr("categories");
                foreach (var kv in groupMaxPerBeaver)
                {
                    float avg = organicBeaverCount > 0 ? wbGroupTotals.GetValueOrDefault(kv.Key) / organicBeaverCount : 0;
                    float max = kv.Value;
                    jj.OpenObj().Prop("group", kv.Key).Prop("current", (float)Math.Round(avg, 1), "F1").Prop("max", (float)Math.Round(max, 1), "F1").CloseObj();
                }
                jj.CloseArr().CloseObj();
                jj.Prop("science", _scienceService.SciencePoints);
                jj.Obj("alerts");
                foreach (var kv in alertCounts) jj.Prop(kv.Key, kv.Value);
                jj.CloseObj();
                jj.Obj("buildings");
                foreach (var kv in roleCounts) jj.Prop(kv.Key, kv.Value);
                jj.CloseObj();
                WriteClustersFiltered(jj, "treeClusters", naturalSnapshot, true, dcX, dcY, dcZ, 40, 10, 5);
                WriteClustersFiltered(jj, "foodClusters", naturalSnapshot, false, dcX, dcY, dcZ, 40, 10, 5);
                return jj.End();
            }

            var jw = _jw.Reset().BeginObj();
            jw.Prop("settlement", GetSettlementName());
            jw.Prop("faction", faction);
            jw.Prop("day", _dayNightCycle.DayNumber);
            jw.Prop("dayProgress", (float)_dayNightCycle.DayProgress);
            jw.Prop("speed", currentSpeed);
            jw.Prop("cycle", (int)_gameCycleService.Cycle);
            jw.Prop("cycleDay", _gameCycleService.CycleDay);
            jw.Prop("isHazardous", _weatherService.IsHazardousWeather);
            jw.Prop("tempDays", _weatherService.TemperateWeatherDuration);
            jw.Prop("hazardDays", _weatherService.HazardousWeatherDuration);
            jw.Prop("markedGrown", treeMarkedGrown);
            jw.Prop("markedSeedling", treeMarkedSeedling);
            jw.Prop("unmarkedGrown", treeUnmarkedGrown);
            jw.Prop("cropReady", cropReady);
            jw.Prop("cropGrowing", cropGrowing);
            int totalFood = 0, totalWater = 0, logStock = 0, plankStock = 0, gearStock = 0;
            _resourceTotals.Clear();
            var resourceTotals = _resourceTotals;
            foreach (var dc in districts)
            {
                if (dc.Resources != null)
                {
                    foreach (var kvp in dc.Resources)
                    {
                        int avail = kvp.Value.available;
                        resourceTotals[kvp.Key] = resourceTotals.GetValueOrDefault(kvp.Key) + avail;
                        if (kvp.Key == "Water") totalWater += avail;
                        else if (kvp.Key == "Berries" || kvp.Key == "Kohlrabi" || kvp.Key == "Carrot" || kvp.Key == "Potato" || kvp.Key == "Wheat" || kvp.Key == "Bread" || kvp.Key == "Cassava" || kvp.Key == "Corn" || kvp.Key == "Eggplant" || kvp.Key == "Soybean" || kvp.Key == "MapleSyrup")
                            totalFood += avail;
                        else if (kvp.Key == "Log") logStock += avail;
                        else if (kvp.Key == "Plank") plankStock += avail;
                        else if (kvp.Key == "Gear") gearStock += avail;
                    }
                }
            }
            jw.Prop("adults", totalAdults);
            jw.Prop("children", totalChildren);
            jw.Prop("bots", totalBots);
            foreach (var kvp in resourceTotals)
                jw.Key(kvp.Key).Int(kvp.Value);
            if (organicTotal > 0)
            {
                jw.Prop("foodDays", (float)((double)totalFood / organicTotal), "F1");
                jw.Prop("waterDays", (float)((double)totalWater / (organicTotal * 2.0)), "F1");
                jw.Prop("logDays", (float)((double)logStock / organicTotal), "F1");
                jw.Prop("plankDays", (float)((double)plankStock / organicTotal), "F1");
                jw.Prop("gearDays", (float)((double)gearStock / organicTotal), "F1");
            }
            jw.Prop("beds", $"{occupiedBeds}/{totalBeds}");
            jw.Prop("homeless", homeless);
            jw.Prop("workers", $"{assignedWorkers}/{totalVacancies}");
            jw.Prop("unemployed", unemployed);
            jw.Prop("wellbeing", avgWellbeing, "F1");
            jw.Prop("miserable", miserable);
            jw.Prop("critical", critical);
            jw.Prop("science", _scienceService.SciencePoints);
            string alertStr = "none";
            if (alertCounts.Count > 0)
            {
                var parts = new List<string>();
                foreach (var kv in alertCounts)
                    parts.Add($"{kv.Value} {kv.Key}");
                alertStr = string.Join(", ", parts);
            }
            jw.Prop("alerts", alertStr);
            jw.Obj("buildings");
            foreach (var kv in roleCounts) jw.Prop(kv.Key, kv.Value);
            jw.CloseObj();
            return jw.End();
        }
        // Derived endpoints: built from published snapshots on the background thread.
        // No main-thread work needed. just reads from the last published data.
        public object CollectAlerts(string format = "toon", int limit = 100, int offset = 0)
            => _alertsRoute.Collect(format, limit, offset);
        // Cluster endpoints: divide the map into cells, count trees/food in each,
        // return the densest N. Used by the AI to find good foresting/gathering spots.
        public object CollectTreeClusters(string format = "toon", int cellSize = 10, int top = 5)
        {
            ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot snapshot;
            try { snapshot = _naturalResourceSnapshot.RequestFresh(false, 2000); }
            catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
            foreach (var kv in _clusterCells) { kv.Value[0] = 0; kv.Value[1] = 0; }
            foreach (var kv in _clusterSpecies) kv.Value.Clear();
            var cells = _clusterCells;
            var cellSpecies = _clusterSpecies;
            for (int i = 0; i < snapshot.Count; i++)
            {
                var d = snapshot.Definitions[i];
                var nr = snapshot.States[i];
                if (d.IsTree == 0) continue;
                if (nr.Alive == 0) continue;
                int cx = nr.X / cellSize * cellSize + cellSize / 2;
                int cy = nr.Y / cellSize * cellSize + cellSize / 2;
                long key = (long)cx * 100000 + cy;
                if (!cells.TryGetValue(key, out var cell))
                { cell = new int[] { 0, 0, cx, cy, nr.Z }; cells[key] = cell; cellSpecies[key] = new Dictionary<string, int>(); }
                else { cell[0] = 0; cell[1] = 0; cell[2] = cx; cell[3] = cy; cell[4] = nr.Z; }
                cells[key][1]++;
                cellSpecies[key][d.Name] = cellSpecies[key].GetValueOrDefault(d.Name) + 1;
                if (nr.Grown != 0) cells[key][0]++;
            }
            _clusterSorted.Clear(); _clusterSorted.AddRange(cells.Keys);
            _clusterSorted.Sort((a, b) => cells[b][0].CompareTo(cells[a][0]));
            var jw = _jw.Reset().BeginArr();
            for (int i = 0; i < Math.Min(top, _clusterSorted.Count); i++)
            {
                var s = cells[_clusterSorted[i]];
                jw.OpenObj().Prop("x", s[2]).Prop("y", s[3]).Prop("z", s[4]).Prop("grown", s[0]).Prop("total", s[1]);
                var sp = cellSpecies[_clusterSorted[i]];
                jw.Obj("species");
                foreach (var kv in sp) jw.Prop(kv.Key, kv.Value);
                jw.CloseObj().CloseObj();
            }
            return jw.End();
        }
        public object CollectFoodClusters(string format = "toon", int cellSize = 10, int top = 5)
        {
            ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot snapshot;
            try { snapshot = _naturalResourceSnapshot.RequestFresh(false, 2000); }
            catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
            foreach (var kv in _clusterCells) { kv.Value[0] = 0; kv.Value[1] = 0; }
            foreach (var kv in _clusterSpecies) kv.Value.Clear();
            var cells = _clusterCells;
            var cellSpecies = _clusterSpecies;
            for (int i = 0; i < snapshot.Count; i++)
            {
                var d = snapshot.Definitions[i];
                var nr = snapshot.States[i];
                if (d.IsGatherable == 0) continue;
                if (nr.Alive == 0) continue;
                int cx = nr.X / cellSize * cellSize + cellSize / 2;
                int cy = nr.Y / cellSize * cellSize + cellSize / 2;
                long key = (long)cx * 100000 + cy;
                if (!cells.TryGetValue(key, out var cell))
                { cell = new int[] { 0, 0, cx, cy, nr.Z }; cells[key] = cell; cellSpecies[key] = new Dictionary<string, int>(); }
                else { cell[0] = 0; cell[1] = 0; cell[2] = cx; cell[3] = cy; cell[4] = nr.Z; }
                cells[key][1]++;
                cellSpecies[key][d.Name] = cellSpecies[key].GetValueOrDefault(d.Name) + 1;
                if (nr.Grown != 0) cells[key][0]++;
            }
            _clusterSorted.Clear(); _clusterSorted.AddRange(cells.Keys);
            _clusterSorted.Sort((a, b) => cells[b][0].CompareTo(cells[a][0]));
            var jw = _jw.Reset().BeginArr();
            for (int i = 0; i < Math.Min(top, _clusterSorted.Count); i++)
            {
                var s = cells[_clusterSorted[i]];
                jw.OpenObj().Prop("x", s[2]).Prop("y", s[3]).Prop("z", s[4]).Prop("grown", s[0]).Prop("total", s[1]);
                var sp = cellSpecies[_clusterSorted[i]];
                jw.Obj("species");
                foreach (var kv in sp) jw.Prop(kv.Key, kv.Value);
                jw.CloseObj().CloseObj();
            }
            return jw.End();
        }
        public object CollectResources(string format = "toon")
        {
            DistrictSnapshot[] districts;
            try { districts = _districtStore.RequestFresh(2000); }
            catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
            var jw = _jw.Reset();
            if (format == "toon")
            {
                jw.OpenArr();
                foreach (var dc in districts)
                {
                    if (dc.Resources == null) continue;
                    foreach (var kvp in dc.Resources)
                        jw.OpenObj().Prop("district", dc.Name).Prop("good", kvp.Key).Prop("available", kvp.Value.available).Prop("all", kvp.Value.all).CloseObj();
                }
                jw.CloseArr();
            }
            else
            {
                jw.OpenObj();
                foreach (var dc in districts)
                {
                    jw.Key(dc.Name).OpenObj();
                    if (dc.ResourcesJson != null) jw.Raw(dc.ResourcesJson);
                    jw.CloseObj();
                }
                jw.CloseObj();
            }
            return jw.ToString();
        }
        public object CollectPopulation()
        {
            DistrictSnapshot[] districts;
            try { districts = _districtStore.RequestFresh(2000); }
            catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
            var jw = _jw.Reset().BeginArr();
            foreach (var dc in districts)
            {
                jw.OpenObj()
                    .Prop("district", dc.Name)
                    .Prop("adults", dc.Adults)
                    .Prop("children", dc.Children)
                    .Prop("bots", dc.Bots)
                    .CloseObj();
            }
            return jw.End();
        }
        public object CollectTime() => _timeRoute.Collect();
        public object CollectWeather() => _weatherRoute.Collect();
        public object CollectDistricts(string format = "toon")
        {
            DistrictSnapshot[] districts;
            try { districts = _districtStore.RequestFresh(2000); }
            catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
            var jw = _jw.Reset().BeginArr();
            foreach (var dc in districts)
            {
                jw.OpenObj().Prop("name", dc.Name);
                if (format == "toon")
                {
                    jw.Prop("adults", dc.Adults).Prop("children", dc.Children).Prop("bots", dc.Bots);
                    if (!string.IsNullOrEmpty(dc.ResourcesToon)) jw.Raw(dc.ResourcesToon);
                }
                else
                {
                    jw.Obj("population")
                        .Prop("adults", dc.Adults).Prop("children", dc.Children).Prop("bots", dc.Bots)
                        .CloseObj();
                    jw.Obj("resources");
                    if (dc.ResourcesJson != null) jw.Raw(dc.ResourcesJson);
                    jw.CloseObj();
                }
                jw.CloseObj();
            }
            return jw.End();
        }
        public object CollectTrees(string format = "toon", int limit = 100, int offset = 0, string filterName = null, int filterX = 0, int filterY = 0, int filterRadius = 0)
            => _treesEndpoint.Collect(format, "basic", 0, limit, offset, filterName, filterX, filterY, filterRadius);
        public object CollectCrops(string format = "toon", int limit = 100, int offset = 0, string filterName = null, int filterX = 0, int filterY = 0, int filterRadius = 0)
            => _cropsEndpoint.Collect(format, "basic", 0, limit, offset, filterName, filterX, filterY, filterRadius);
        public object CollectGatherables(string format = "toon", int limit = 100, int offset = 0, string filterName = null, int filterX = 0, int filterY = 0, int filterRadius = 0)
            => _gatherablesEndpoint.Collect(format, "basic", 0, limit, offset, filterName, filterX, filterY, filterRadius);
        public object CollectBeavers(string format = "toon", string detail = "basic", int id = 0, int limit = 100, int offset = 0, string filterName = null, int filterX = 0, int filterY = 0, int filterRadius = 0)
            => _beaversEndpoint.Collect(format, detail, id, limit, offset, filterName, filterX, filterY, filterRadius);
        public object CollectDistribution(string format = "toon") => _distributionRoute.Collect(format);
        public object CollectScience(string format = "toon") => _scienceRoute.Collect(format);
        // CollectWellbeing: aggregate wellbeing by category across all beavers.
        // Groups needs by Timberborn's wellbeing categories (Social, Fun, Nutrition,
        // Aesthetics, Awe, etc.) and averages across the population.
        public object CollectWellbeing(string format = "toon")
        {
            try
            {
                ProjectionSnapshot<BeaverDefinition, BeaverState, BeaverDetailState>.Snapshot beavers;
                try { beavers = _beaverSnapshot.RequestFresh(true, 2000); }
                catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
                var beaverNeeds = _cachedBeaverNeeds;
                foreach (var kv in _wbGroupNeeds) kv.Value.Clear();
                _wbGroupMaxTotals.Clear();
                _needToGroup.Clear();
                _wbGroupTotals.Clear();
                var groupNeeds = _wbGroupNeeds;
                foreach (var ns in beaverNeeds)
                {
                    var groupId = ns.NeedGroupId;
                    if (string.IsNullOrEmpty(groupId)) continue;
                    if (!groupNeeds.TryGetValue(groupId, out var list))
                    { list = new List<NeedSpec>(); groupNeeds[groupId] = list; }
                    list.Add(ns);
                }
                int beaverCount = 0;
                var groupTotals = _wbGroupTotals;
                var groupMaxTotals = _wbGroupMaxTotals;
                var needToGroup = _needToGroup;
                foreach (var kvp in groupNeeds)
                    foreach (var ns in kvp.Value)
                        needToGroup[ns.Id] = kvp.Key;
                for (int i = 0; i < beavers.Count; i++)
                {
                    var d = beavers.Definitions[i];
                    if (d.IsBot != 0) continue;
                    var detailState = beavers.Details?[i];
                    if (detailState?.Needs == null) continue;
                    beaverCount++;
                    foreach (var n in detailState.Needs)
                    {
                        if (!needToGroup.TryGetValue(n.Id, out var groupId)) continue;
                        if (!groupTotals.ContainsKey(groupId)) { groupTotals[groupId] = 0f; groupMaxTotals[groupId] = 0f; }
                        groupTotals[groupId] += n.Wellbeing;
                    }
                    foreach (var kvp in groupNeeds)
                    {
                        var groupId = kvp.Key;
                        float groupMax = 0f;
                        foreach (var ns in kvp.Value) groupMax += ns.FavorableWellbeing;
                        if (!groupMaxTotals.ContainsKey(groupId)) groupMaxTotals[groupId] = 0f;
                        groupMaxTotals[groupId] += groupMax;
                    }
                }
                var jw = _jw.Reset().BeginObj().Prop("beavers", beaverCount).Arr("categories");
                foreach (var kvp in groupNeeds)
                {
                    var groupId = kvp.Key;
                    float avgCurrent = beaverCount > 0 ? groupTotals.GetValueOrDefault(groupId) / beaverCount : 0;
                    float avgMax = beaverCount > 0 ? groupMaxTotals.GetValueOrDefault(groupId) / beaverCount : 0;
                    jw.OpenObj().Prop("group", groupId).Prop("current", (float)Math.Round(avgCurrent, 1), "F1").Prop("max", (float)Math.Round(avgMax, 1), "F1");
                    jw.Arr("needs");
                    foreach (var ns in kvp.Value)
                        jw.OpenObj().Prop("id", ns.Id).Prop("favorableWellbeing", ns.FavorableWellbeing, "F1").Prop("unfavorableWellbeing", ns.UnfavorableWellbeing, "F1").CloseObj();
                    jw.CloseArr().CloseObj();
                }
                jw.CloseArr().CloseObj();
                return jw.ToString();
            }
            catch (Exception ex) { TimberbotLog.Error("wellbeing", ex); return _jw.Error("operation_failed: " + ex.Message); }
        }
        public object CollectNotifications(string format = "toon", int limit = 100, int offset = 0)
            => _notificationsRoute.Collect(format, limit, offset);
        public object CollectWorkHours() => _workHoursRoute.Collect();
        public object CollectPowerNetworks(string format = "toon") => _powerRoute.Collect(format);
        public object CollectSpeed() => _speedRoute.Collect();
        // CollectTiles: raw map data for a region. Unlike other endpoints, this reads
        // directly from thread-safe game services (IThreadSafeWaterMap, terrain) plus
        // the building/resource snapshots for occupant data. This is what powers the
        // ASCII map and the AI's spatial reasoning about water depth and contamination.
        public object CollectTiles(string format = "toon", int x1 = 0, int y1 = 0, int x2 = 0, int y2 = 0)
        {
            ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>.Snapshot buildingSnapshot;
            ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot naturalSnapshot;
            var size = _terrainService.Size;
            var stride = _mapIndexService.VerticalStride;

            if (x1 == 0 && y1 == 0 && x2 == 0 && y2 == 0)
                return _jw.Reset().BeginObj().Obj("mapSize").Prop("x", size.x).Prop("y", size.y).Prop("z", size.z).CloseObj().CloseObj().ToString();

            x1 = Mathf.Clamp(x1, 0, size.x - 1);
            y1 = Mathf.Clamp(y1, 0, size.y - 1);
            x2 = Mathf.Clamp(x2, 0, size.x - 1);
            y2 = Mathf.Clamp(y2, 0, size.y - 1);

            foreach (var kv in _tileOccupants) kv.Value.Clear();
            _tileEntrances.Clear();
            _tileSeedlings.Clear();
            _tileDeadTiles.Clear();
            var occupants = _tileOccupants;
            var entrances = _tileEntrances;
            var seedlings = _tileSeedlings;
            var deadTiles = _tileDeadTiles;

            try
            {
                buildingSnapshot = _snapshot.RequestFresh(false, 2000);
                naturalSnapshot = _naturalResourceSnapshot.RequestFresh(false, 2000);
            }
            catch (TimeoutException)
            {
                return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s");
            }

            for (int i = 0; i < buildingSnapshot.Count; i++)
            {
                var c = buildingSnapshot.Definitions[i];
                if (c.OccupiedTiles == null) continue;
                foreach (var tile in c.OccupiedTiles)
                {
                    if (tile.x >= x1 && tile.x <= x2 && tile.y >= y1 && tile.y <= y2)
                    {
                        long key = (long)tile.x * 100000 + tile.y;
                        if (!occupants.TryGetValue(key, out var occList))
                        { occList = new List<(string, int)>(); occupants[key] = occList; }
                        occList.Add((c.Name, tile.z));
                    }
                }
                if (c.HasEntrance != 0 && c.EntranceX >= x1 && c.EntranceX <= x2 && c.EntranceY >= y1 && c.EntranceY <= y2)
                    entrances.Add((long)c.EntranceX * 100000 + c.EntranceY);
            }

            for (int i = 0; i < naturalSnapshot.Count; i++)
            {
                var d = naturalSnapshot.Definitions[i];
                var c = naturalSnapshot.States[i];
                if (c.X < x1 || c.X > x2 || c.Y < y1 || c.Y > y2) continue;
                long key = (long)c.X * 100000 + c.Y;
                if (c.Grown == 0 && c.Alive != 0) seedlings.Add(key);
                if (c.Alive == 0) deadTiles.Add(key);
                if (!occupants.TryGetValue(key, out var nrOccList))
                { nrOccList = new List<(string, int)>(); occupants[key] = nrOccList; }
                nrOccList.Add((d.Name, c.Z));
            }

            for (int i = 0; i < _trackedBlockers.Count; i++)
            {
                var b = _trackedBlockers[i];
                if (b.OccupiedTiles == null) continue;
                foreach (var tile in b.OccupiedTiles)
                {
                    if (tile.x >= x1 && tile.x <= x2 && tile.y >= y1 && tile.y <= y2)
                    {
                        long key = (long)tile.x * 100000 + tile.y;
                        if (!occupants.TryGetValue(key, out var blkOccList))
                        { blkOccList = new List<(string, int)>(); occupants[key] = blkOccList; }
                        blkOccList.Add((b.Name, tile.z));
                    }
                }
            }

            var jw = _jw.Reset().BeginObj();
            jw.Obj("mapSize").Prop("x", size.x).Prop("y", size.y).Prop("z", size.z).CloseObj();
            jw.Obj("region").Prop("x1", x1).Prop("y1", y1).Prop("x2", x2).Prop("y2", y2).CloseObj();
            jw.Arr("tiles");

            for (int y = y1; y <= y2; y++)
            {
                for (int x = x1; x <= x2; x++)
                {
                    int terrainHeight = 0;
                    float waterDepth = 0f;
                    float waterContamination = 0f;

                    var index2D = _mapIndexService.CellToIndex(new Vector2Int(x, y));
                    var columnCount = _terrainMap.ColumnCounts[index2D];
                    if (columnCount > 0)
                    {
                        var topIndex = (columnCount - 1) * stride + index2D;
                        terrainHeight = _terrainMap.GetColumnCeiling(topIndex);
                    }

                    int wIdx2D = _mapIndexService.CellToIndex(new Vector2Int(x, y));
                    int wColCount = _waterMap.ColumnCount(wIdx2D);
                    for (int ci = 0; ci < wColCount; ci++)
                    {
                        int wIdx3D = ci * _mapIndexService.VerticalStride + wIdx2D;
                        var col = _waterMap.WaterColumns[wIdx3D];
                        if (col.Ceiling >= terrainHeight)
                        {
                            waterDepth = col.WaterDepth;
                            waterContamination = _waterMap.ColumnContamination(new Vector3Int(x, y, col.Ceiling));
                            break;
                        }
                    }

                    long key = (long)x * 100000 + y;
                    int contaminated = 0, moist = 0;
                    try { contaminated = _soilContaminationService.SoilIsContaminated(new Vector3Int(x, y, terrainHeight)) ? 1 : 0; } catch (Exception ex) { TimberbotLog.Error("map.soil", ex); }
                    try { moist = _soilMoistureService.SoilIsMoist(new Vector3Int(x, y, terrainHeight)) ? 1 : 0; } catch (Exception ex) { TimberbotLog.Error("map.moisture", ex); }

                    jw.OpenObj().Prop("x", x).Prop("y", y).Prop("terrain", terrainHeight);
                    jw.Prop("water", waterDepth, "F1");
                    jw.Prop("badwater", (float)Math.Round(waterContamination, 2));
                    jw.Prop("entrance", entrances.Contains(key) ? 1 : 0);
                    jw.Prop("seedling", seedlings.Contains(key) ? 1 : 0);
                    jw.Prop("dead", deadTiles.Contains(key) ? 1 : 0);
                    jw.Prop("contaminated", contaminated);
                    jw.Prop("moist", moist);

                    if (format == "toon")
                    {
                        _tileSb.Clear();
                        var occList = occupants.ContainsKey(key) ? occupants[key] : null;
                        if (occList != null)
                        {
                            for (int oi = 0; oi < occList.Count; oi++)
                            {
                                if (oi > 0) _tileSb.Append('+');
                                _tileSb.Append(occList[oi].name);
                                _tileSb.Append(':');
                                _tileSb.Append(occList[oi].z);
                            }
                        }
                        jw.Prop("occupants", _tileSb.ToString());
                    }
                    else
                    {
                        jw.Arr("occupants");
                        var occList = occupants.ContainsKey(key) ? occupants[key] : null;
                        if (occList != null) foreach (var o in occList) jw.OpenObj().Prop("name", o.name).Prop("z", o.z).CloseObj();
                        jw.CloseArr();
                    }
                    jw.CloseObj();
                }
            }
            jw.CloseArr().CloseObj();
            return jw.ToString();
        }
        public string GetSettlementName()
        {
            try
            {
                return _settlementStore.RequestFresh(2000)?.Name ?? "unknown";
            }
            catch (TimeoutException)
            {
                return "unknown";
            }
        }

        private SettlementSnapshot BuildSettlementSnapshot()
        {
            return new SettlementSnapshot { Name = ResolveSettlementName() };
        }

        private TimeSnapshot BuildTimeSnapshot()
        {
            return new TimeSnapshot
            {
                DayNumber = _dayNightCycle.DayNumber,
                DayProgress = _dayNightCycle.DayProgress,
                PartialDayNumber = _dayNightCycle.PartialDayNumber
            };
        }

        private WeatherSnapshot BuildWeatherSnapshot()
        {
            return new WeatherSnapshot
            {
                Cycle = (int)_gameCycleService.Cycle,
                CycleDay = _gameCycleService.CycleDay,
                IsHazardous = _weatherService.IsHazardousWeather,
                TemperateWeatherDuration = _weatherService.TemperateWeatherDuration,
                HazardousWeatherDuration = _weatherService.HazardousWeatherDuration,
                CycleLengthInDays = _weatherService.CycleLengthInDays
            };
        }

        private SpeedSnapshot BuildSpeedSnapshot()
        {
            var raw = _speedManager.CurrentSpeed;
            int level = Array.IndexOf(SpeedScale, (int)raw);
            if (level < 0) level = 0;
            return new SpeedSnapshot { Speed = level };
        }

        private WorkHoursSnapshot BuildWorkHoursSnapshot()
        {
            return new WorkHoursSnapshot
            {
                EndHours = _workingHoursManager.EndHours,
                AreWorkingHours = _workingHoursManager.AreWorkingHours
            };
        }

        private ScienceCapture CaptureScienceSnapshot()
        {
            var unlockables = new List<ScienceUnlockableCapture>();
            foreach (var building in _buildingService.Buildings)
            {
                var bs = building.GetSpec<BuildingSpec>();
                if (bs == null || bs.ScienceCost <= 0) continue;
                var templateSpec = building.GetSpec<Timberborn.TemplateSystem.TemplateSpec>();
                var name = templateSpec?.TemplateName ?? "unknown";
                unlockables.Add(new ScienceUnlockableCapture
                {
                    Name = name,
                    Cost = bs.ScienceCost,
                    Unlocked = _buildingUnlockingService.Unlocked(bs) ? 1 : 0
                });
            }
            return new ScienceCapture
            {
                Points = _scienceService.SciencePoints,
                Unlockables = unlockables.ToArray()
            };
        }

        private RawJsonSnapshot FinalizeScienceSnapshot(ScienceCapture capture)
        {
            var jw = new TimberbotJw(4096).Reset().OpenObj().Prop("points", capture.Points);
            jw.Arr("unlockables");
            for (int i = 0; i < capture.Unlockables.Length; i++)
            {
                var unlockable = capture.Unlockables[i];
                jw.OpenObj()
                    .Prop("name", unlockable.Name)
                    .Prop("cost", unlockable.Cost)
                    .Prop("unlocked", unlockable.Unlocked)
                    .CloseObj();
            }
            jw.CloseArr().CloseObj();
            return new RawJsonSnapshot { Json = jw.ToString() };
        }

        private DistributionCapture CaptureDistributionSnapshot()
        {
            var districts = new List<DistributionDistrictCapture>();
            foreach (var dc in _districtCenterRegistry.FinishedDistrictCenters)
            {
                var distSetting = dc.GetComponent<Timberborn.DistributionSystem.DistrictDistributionSetting>();
                if (distSetting == null) continue;
                var goods = new DistributionGoodCapture[distSetting.GoodDistributionSettings.Count];
                for (int i = 0; i < distSetting.GoodDistributionSettings.Count; i++)
                {
                    var gs = distSetting.GoodDistributionSettings[i];
                    goods[i] = new DistributionGoodCapture
                    {
                        Good = gs.GoodId,
                        ImportOption = gs.ImportOption.ToString(),
                        ExportThreshold = gs.ExportThreshold
                    };
                }
                districts.Add(new DistributionDistrictCapture
                {
                    District = dc.DistrictName,
                    Goods = goods
                });
            }
            return new DistributionCapture { Districts = districts.ToArray() };
        }

        private RawJsonSnapshot FinalizeDistributionSnapshot(DistributionCapture capture)
        {
            var jw = new TimberbotJw(4096).BeginArr();
            for (int di = 0; di < capture.Districts.Length; di++)
            {
                var district = capture.Districts[di];
                jw.OpenObj().Prop("district", district.District).Arr("goods");
                for (int gi = 0; gi < district.Goods.Length; gi++)
                {
                    var good = district.Goods[gi];
                    jw.OpenObj()
                        .Prop("good", good.Good)
                        .Prop("importOption", good.ImportOption)
                        .Prop("exportThreshold", good.ExportThreshold, "F0")
                        .CloseObj();
                }
                jw.CloseArr().CloseObj();
            }
            return new RawJsonSnapshot { Json = jw.End() };
        }

        private NotificationItem[] BuildNotificationsSnapshot()
        {
            try
            {
                var items = new List<NotificationItem>();
                foreach (var n in _notificationSaver.Notifications)
                {
                    items.Add(new NotificationItem
                    {
                        Subject = n.Subject.ToString(),
                        Description = n.Description.ToString(),
                        Cycle = n.Cycle,
                        CycleDay = n.CycleDay
                    });
                }
                return items.ToArray();
            }
            catch (Exception ex)
            {
                TimberbotLog.Error("readv2.notifications", ex);
                return Array.Empty<NotificationItem>();
            }
        }

        private static T IdentitySnapshot<T>(T snapshot) where T : class => snapshot;

        private void FinalizeBuildingSnapshot(ProjectionSnapshot<BuildingDefinition, BuildingState, BuildingDetailState>.Buffer buffer, int count, bool fullDetail)
        {
            if (!fullDetail) return;
            for (int i = 0; i < count; i++)
            {
                var detail = buffer.Details[i];
                detail.InventoryToon = TimberbotPure.ToToonDict(detail.Inventory);
                var sb = new StringBuilder(128);
                for (int ri = 0; ri < detail.Recipes.Count; ri++)
                {
                    if (ri > 0) sb.Append('/');
                    sb.Append(detail.Recipes[ri]);
                }
                detail.RecipesToon = sb.ToString();
            }
        }

        private readonly List<AlertItem> _alertBuffer = new List<AlertItem>();
        private AlertItem[] BuildAlertsFromBuildings()
        {
            var snapshot = _snapshot.RequestFresh(false, 2000);
            _alertBuffer.Clear();
            var alerts = _alertBuffer;
            for (int i = 0; i < snapshot.Count; i++)
            {
                var d = snapshot.Definitions[i];
                var s = snapshot.States[i];
                if (s.StatusAlerts != null && s.StatusAlerts.Length > 0)
                {
                    for (int ai = 0; ai < s.StatusAlerts.Length; ai++)
                    {
                        alerts.Add(new AlertItem
                        {
                            Type = s.StatusAlerts[ai],
                            Id = d.Id,
                            Name = d.Name,
                            Workers = s.MaxWorkers > 0 ? $"{s.AssignedWorkers}/{s.DesiredWorkers}" : ""
                        });
                    }
                }
            }
            return alerts.ToArray();
        }

        private readonly Dictionary<int, PowerNetworkBuilder> _powerNetworks = new Dictionary<int, PowerNetworkBuilder>();
        private PowerNetworkItem[] BuildPowerFromBuildings()
        {
            var snapshot = _snapshot.RequestFresh(false, 2000);
            _powerNetworks.Clear();
            var networks = _powerNetworks;
            for (int i = 0; i < snapshot.Count; i++)
            {
                var d = snapshot.Definitions[i];
                var s = snapshot.States[i];
                if (s.PowerNetworkId == 0) continue;
                if (!networks.TryGetValue(s.PowerNetworkId, out var builder))
                {
                    builder = new PowerNetworkBuilder
                    {
                        Id = s.PowerNetworkId,
                        Supply = s.PowerSupply,
                        Demand = s.PowerDemand,
                        Buildings = new List<PowerBuildingItem>()
                    };
                    networks[s.PowerNetworkId] = builder;
                }
                builder.Buildings.Add(new PowerBuildingItem
                {
                    Name = d.Name,
                    Id = d.Id,
                    IsGenerator = d.IsGenerator,
                    NominalOutput = d.NominalPowerOutput,
                    NominalInput = d.NominalPowerInput
                });
            }

            var result = new PowerNetworkItem[networks.Count];
            int index = 0;
            foreach (var builder in networks.Values)
            {
                result[index++] = new PowerNetworkItem
                {
                    Id = builder.Id,
                    Supply = builder.Supply,
                    Demand = builder.Demand,
                    Buildings = builder.Buildings.ToArray()
                };
            }
            return result;
        }

        private string ResolveSettlementName()
        {
            try
            {
                var obj = (object)_gameCycleService;
                foreach (var field in new[] { "_singletonLoader", "_serializedWorldSupplier", "_sceneLoader", "_sceneParameters" })
                {
                    var fi = obj.GetType().GetField(field, System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                    if (fi == null) return "unknown";
                    obj = fi.GetValue(obj);
                    if (obj == null) return "unknown";
                }
                var saveRef = obj.GetType().GetProperty("SaveReference")?.GetValue(obj);
                if (saveRef == null) return "unknown";
                var settRef = saveRef.GetType().GetProperty("SettlementReference")?.GetValue(saveRef);
                if (settRef == null) return "unknown";
                var name = settRef.GetType().GetProperty("SettlementName")?.GetValue(settRef);
                return name?.ToString() ?? "unknown";
            }
            catch
            {
                return "unknown";
            }
        }

        private void RefreshState(BuildingState s, TrackedBuildingRef t)
        {
            var bo = t.BlockObject;
            s.Finished = bo != null && bo.IsFinished ? 1 : 0;
            s.Pausable = t.Pausable != null ? 1 : 0;
            s.Paused = t.Pausable != null && t.Pausable.Paused ? 1 : 0;
            s.Unreachable = t.Reachability != null && t.Reachability.IsAnyUnreachable() ? 1 : 0;
            s.Reachable = t.Reachability != null ? (s.Unreachable == 0 ? 1 : 0) : 0;
            s.Powered = t.Mechanical != null && t.Mechanical.ActiveAndPowered ? 1 : 0;
            s.District = t.DistrictBuilding != null ? t.DistrictBuilding.District?.DistrictName : null;
            if (t.Workplace != null)
            {
                s.AssignedWorkers = t.Workplace.NumberOfAssignedWorkers;
                s.DesiredWorkers = t.Workplace.DesiredWorkers;
                s.MaxWorkers = t.Workplace.MaxWorkers;
            }
            else { s.AssignedWorkers = 0; s.DesiredWorkers = 0; s.MaxWorkers = 0; }
            if (t.Dwelling != null)
            {
                s.Dwellers = t.Dwelling.NumberOfDwellers;
                s.MaxDwellers = t.Dwelling.MaxBeavers;
            }
            else { s.Dwellers = 0; s.MaxDwellers = 0; }
            s.FloodgateHeight = t.Floodgate != null ? t.Floodgate.Height : 0f;
            s.ConstructionPriority = t.BuilderPrio != null ? TimberbotEntityRegistry.GetPriorityName(t.BuilderPrio.Priority) : null;
            s.WorkplacePriorityStr = t.WorkplacePrio != null ? TimberbotEntityRegistry.GetPriorityName(t.WorkplacePrio.Priority) : null;
            if (t.Site != null)
            {
                s.BuildProgress = t.Site.BuildTimeProgress;
                s.MaterialProgress = t.Site.MaterialProgress;
                s.HasMaterials = t.Site.HasMaterialsToResumeBuilding ? 1 : 0;
            }
            else { s.BuildProgress = 0f; s.MaterialProgress = 0f; s.HasMaterials = 0; }
            s.ClutchEngaged = t.Clutch != null && t.Clutch.IsEngaged ? 1 : 0;
            s.WonderActive = t.Wonder != null && t.Wonder.IsActive ? 1 : 0;
            s.PowerDemand = 0; s.PowerSupply = 0; s.PowerNetworkId = 0;
            if (t.PowerNode != null)
            {
                try
                {
                    var g = t.PowerNode.Graph;
                    if (g != null)
                    {
                        s.PowerDemand = (int)g.PowerDemand;
                        s.PowerSupply = (int)g.PowerSupply;
                        s.PowerNetworkId = System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(g);
                    }
                }
                catch (Exception ex) { TimberbotLog.Error("readv2.power", ex); }
            }
            if (t.Manufactory != null)
            {
                s.CurrentRecipe = t.Manufactory.HasCurrentRecipe ? t.Manufactory.CurrentRecipe.Id : "";
                s.ProductionProgress = t.Manufactory.ProductionProgress;
                s.ReadyToProduce = t.Manufactory.IsReadyToProduce ? 1 : 0;
            }
            else { s.CurrentRecipe = ""; s.ProductionProgress = 0f; s.ReadyToProduce = 0; }
            s.NeedsNutrients = t.BreedingPod != null && t.BreedingPod.NeedsNutrients ? 1 : 0;
            s.Nutrients = 0;
            if (t.BreedingPod != null)
            {
                try
                {
                    foreach (var ga in t.BreedingPod.Nutrients)
                        s.Nutrients += ga.Amount;
                }
                catch (Exception ex) { TimberbotLog.Error("readv2.nutrients_state", ex); }
            }
            s.Stock = 0; s.Capacity = 0;
            if (t.Inventories != null)
            {
                try
                {
                    var allInv = t.Inventories.AllInventories;
                    for (int ii = 0; ii < allInv.Count; ii++)
                    {
                        var inv = allInv[ii];
                        if (inv.ComponentName == ConstructionSiteInventoryInitializer.InventoryComponentName) continue;
                        s.Stock += inv.TotalAmountInStock;
                        s.Capacity += inv.Capacity;
                    }
                }
                catch (Exception ex) { TimberbotLog.Error("readv2.stock", ex); }
            }
            s.StorageMode = "";
            s.AllowedGood = "";
            if (t.StoragePriority != null)
            {
                s.StorageMode = t.StoragePriority.IsEmptyActive ? "empty" : t.StoragePriority.IsObtainActive ? "obtain" : t.StoragePriority.IsSupplyActive ? "supply" : "accept";
            }
            if (t.GoodAllower != null && t.GoodAllower.HasAllowedGood)
            {
                s.AllowedGood = t.GoodAllower.AllowedGood;
            }
            s.StatusAlerts = System.Array.Empty<string>();
            if (t.Status != null)
            {
                try
                {
                    var active = t.Status.ActiveStatuses;
                    if (active.Count > 0)
                    {
                        var arr = new string[active.Count];
                        for (int si = 0; si < active.Count; si++)
                            arr[si] = active[si].StatusDescription;
                        s.StatusAlerts = arr;
                    }
                }
                catch (Exception ex) { TimberbotLog.Error("readv2.status", ex); }
            }
            RefreshAutomationState(s, t);
        }

        private void RefreshAutomationState(BuildingState s, TrackedBuildingRef t)
        {
            s.HasAutomator = t.Automator != null ? 1 : 0;
            s.IsTransmitter = t.Automator != null && t.Automator.IsTransmitter ? 1 : 0;
            try { s.AutomatorState = t.Automator != null ? t.Automator.State.ToString() : ""; } catch { s.AutomatorState = "Off"; }
            
            try { s.IsAutomated = t.Automatable != null && t.Automatable.IsAutomated ? 1 : 0; } catch { s.IsAutomated = 0; }

            try { s.InputName = t.Automatable?.Input?.AutomatorName ?? ""; } catch { s.InputName = ""; }

            s.AutomatorName = t.Automator?.AutomatorName ?? "";
            s.AutomationType = TimberbotPure.DetermineAutomationType(
                t.Relay != null, t.Memory != null, t.Lever != null, t.Chronometer != null,
                t.DepthSensor != null, t.ContaminationSensor != null, t.FlowSensor != null,
                t.ResourceCounter != null, t.PopulationCounter != null, t.PowerMeter != null,
                t.Automatable != null);
            
            try { s.AutomationConfig = DetermineAutomationConfig(t); }
            catch { s.AutomationConfig = "{}"; }

            try { s.AutomationInputsJson = DetermineAutomationInputs(t); }
            catch { s.AutomationInputsJson = "[]"; }

            try { s.AutomationOutputsJson = DetermineAutomationOutputs(t); }
            catch { s.AutomationOutputsJson = "[]"; }
        }

        private string DetermineAutomationInputs(TrackedBuildingRef t)
        {
            _configJw.Reset().BeginArr();
            bool has = false;

            void AddInput(string key, Automator aut)
            {
                if (aut == null) return;
                var ec = aut.GetComponent<EntityComponent>();
                if (ec == null) return;
                _configJw.BeginObj()
                    .Prop("key", key)
                    .Prop("id", _cache.GetLegacyId(ec))
                    .Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name))
                    .CloseObj();
                has = true;
            }

            if (t.Relay != null)
            {
                AddInput("a", t.Relay.InputA);
                AddInput("b", t.Relay.InputB);
            }
            else if (t.Memory != null)
            {
                AddInput("a", t.Memory.InputA);
                AddInput("b", t.Memory.InputB);
                AddInput("reset", t.Memory.ResetInput);
            }
            else if (t.Automatable != null)
            {
                AddInput("input", t.Automatable.Input);
            }

            return has ? _configJw.CloseArr().ToString() : "[]";
        }

        private string DetermineAutomationOutputs(TrackedBuildingRef t)
        {
            if (t.Automator == null) return "[]";

            _configJw.Reset().BeginArr();
            bool has = false;
            foreach (var conn in t.Automator.OutputConnections)
            {
                if (conn?.Receiver != null)
                {
                    var ec = conn.Receiver.GetComponent<EntityComponent>();
                    if (ec != null)
                    {
                        _configJw.BeginObj()
                            .Prop("id", _cache.GetLegacyId(ec))
                            .Prop("name", TimberbotEntityRegistry.CanonicalName(ec.GameObject.name))
                            .CloseObj();
                        has = true;
                    }
                }
            }
            return has ? _configJw.CloseArr().ToString() : "[]";
        }

        private string DetermineAutomationConfig(TrackedBuildingRef t)
        {
            try
            {
                if (t.DepthSensor != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("threshold", t.DepthSensor.Threshold)
                        .Prop("mode", t.DepthSensor.Mode.ToString())
                        .CloseObj().ToString();
                if (t.ContaminationSensor != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("threshold", t.ContaminationSensor.Threshold)
                        .Prop("mode", t.ContaminationSensor.Mode.ToString())
                        .CloseObj().ToString();
                if (t.FlowSensor != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("threshold", t.FlowSensor.Threshold)
                        .Prop("mode", t.FlowSensor.Mode.ToString())
                        .CloseObj().ToString();
                if (t.ResourceCounter != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("goodId", t.ResourceCounter.GoodId)
                        .Prop("threshold", t.ResourceCounter.Threshold)
                        .Prop("fillRateThreshold", t.ResourceCounter.FillRateThreshold)
                        .Prop("mode", t.ResourceCounter.Mode.ToString())
                        .Prop("comparisonMode", t.ResourceCounter.ComparisonMode.ToString())
                        .CloseObj().ToString();
                if (t.PopulationCounter != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("threshold", t.PopulationCounter.Threshold)
                        .Prop("mode", t.PopulationCounter.Mode.ToString())
                        .Prop("comparisonMode", t.PopulationCounter.ComparisonMode.ToString())
                        .CloseObj().ToString();
                if (t.PowerMeter != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("mode", t.PowerMeter.Mode.ToString())
                        .Prop("intThreshold", t.PowerMeter.IntThreshold)
                        .Prop("percentThreshold", t.PowerMeter.PercentThreshold)
                        .Prop("comparisonMode", t.PowerMeter.ComparisonMode.ToString())
                        .CloseObj().ToString();
                if (t.Relay != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("mode", t.Relay.Mode.ToString())
                        .CloseObj().ToString();
                if (t.Memory != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("mode", t.Memory.Mode.ToString())
                        .CloseObj().ToString();
                if (t.Chronometer != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("startTime", t.Chronometer.StartTime)
                        .Prop("endTime", t.Chronometer.EndTime)
                        .Prop("mode", t.Chronometer.Mode.ToString())
                        .CloseObj().ToString();
                if (t.Lever != null)
                    return _configJw.Reset().BeginObj()
                        .Prop("springReturn", t.Lever.IsSpringReturn)
                        .Prop("pinned", t.Lever.IsPinned)
                        .CloseObj().ToString();
            }
            catch { }
            return "{}";
        }

        private void RefreshDetail(BuildingDetailState d, TrackedBuildingRef t)
        {
            d.Inventory.Clear();
            if (t.Inventories != null)
            {
                try
                {
                    var allInv = t.Inventories.AllInventories;
                    for (int ii = 0; ii < allInv.Count; ii++)
                    {
                        var item = allInv[ii];
                        if (item.ComponentName == ConstructionSiteInventoryInitializer.InventoryComponentName) continue;
                        var stock = item.Stock;
                        for (int si = 0; si < stock.Count; si++)
                        {
                            var ga = stock[si];
                            if (ga.Amount <= 0) continue;
                            if (d.Inventory.ContainsKey(ga.GoodId)) d.Inventory[ga.GoodId] += ga.Amount;
                            else d.Inventory[ga.GoodId] = ga.Amount;
                        }
                    }
                }
                catch (Exception ex) { TimberbotLog.Error("readv2.inventory", ex); }
            }
            d.Recipes.Clear();
            if (t.Manufactory != null)
            {
                foreach (var r in t.Manufactory.ProductionRecipes)
                    d.Recipes.Add(r.Id);
            }
            d.NutrientStock.Clear();
            if (t.BreedingPod != null)
            {
                try
                {
                    foreach (var ga in t.BreedingPod.Nutrients)
                        if (ga.Amount > 0) d.NutrientStock[ga.GoodId] = ga.Amount;
                }
                catch (Exception ex) { TimberbotLog.Error("readv2.nutrients", ex); }
            }
            }

            private static void RefreshState(BeaverState s, TrackedBeaverRef t)

        {
            if (t.WbTracker != null)
                s.Wellbeing = t.WbTracker.Wellbeing;
            else
                s.Wellbeing = 0f;

            if (t.Go != null)
            {
                var pos = t.Go.transform.position;
                s.X = Mathf.FloorToInt(pos.x);
                s.Y = Mathf.FloorToInt(pos.z);
                s.Z = Mathf.FloorToInt(pos.y);
            }
            else
            {
                s.X = 0; s.Y = 0; s.Z = 0;
            }

            var wp = t.Worker?.Workplace;
            s.Workplace = wp != null ? TimberbotEntityRegistry.CanonicalName(wp.GameObject.name) : null;
            var dc = t.Citizen?.AssignedDistrict;
            s.District = dc?.DistrictName;
            s.HasHome = t.Dweller != null && t.Dweller.HasHome ? 1 : 0;
            s.Contaminated = t.Contaminable != null && t.Contaminable.IsContaminated ? 1 : 0;
            s.LifeProgress = t.Life != null ? t.Life.LifeProgress : 0f;
            s.DeteriorationProgress = t.Deteriorable != null ? t.Deteriorable.DeteriorationProgress : 0f;
            s.LiftingCapacity = t.Carrier != null ? t.Carrier.LiftingCapacity : 0;
            s.Overburdened = t.Carrier != null && t.Carrier.IsMovementSlowed ? 1 : 0;
            if (t.Carrier != null && t.Carrier.IsCarrying)
            {
                var ga = t.Carrier.CarriedGoods;
                s.IsCarrying = 1;
                s.CarryingGood = ga.GoodId;
                s.CarryAmount = ga.Amount;
            }
            else
            {
                s.IsCarrying = 0;
                s.CarryingGood = "";
                s.CarryAmount = 0;
            }

            s.AnyCritical = 0;
            s.Critical = "";
            s.Unmet = "";
            if (t.NeedMgr != null)
            {
                foreach (var ns in t.NeedMgr.GetNeeds())
                {
                    var need = t.NeedMgr.GetNeed(ns.Id);
                    if (need.IsCritical)
                    {
                        s.Critical = s.Critical.Length > 0 ? s.Critical + "+" + ns.Id : ns.Id;
                        s.AnyCritical = 1;
                    }
                    else if (!need.IsFavorable && need.IsActive)
                    {
                        s.Unmet = s.Unmet.Length > 0 ? s.Unmet + "+" + ns.Id : ns.Id;
                    }
                }
            }
        }

        private static void RefreshDetail(BeaverDetailState d, TrackedBeaverRef t)
        {
            d.Needs.Clear();
            if (t.NeedMgr == null) return;

            foreach (var ns in t.NeedMgr.GetNeeds())
            {
                var need = t.NeedMgr.GetNeed(ns.Id);
                d.Needs.Add(new BeaverNeed
                {
                    Id = ns.Id,
                    Points = (float)Math.Round(need.Points, 2),
                    Wellbeing = t.NeedMgr.GetNeedWellbeing(ns.Id),
                    Favorable = need.IsFavorable ? 1 : 0,
                    Critical = need.IsCritical ? 1 : 0,
                    Active = need.IsActive ? 1 : 0,
                    Group = ns.NeedGroupId ?? ""
                });
            }
        }

        private void RefreshState(NaturalResourceState s, TrackedNaturalResourceRef t)
        {
            if (t.BlockObject != null)
            {
                var coords = t.BlockObject.Coordinates;
                s.X = coords.x;
                s.Y = coords.y;
                s.Z = coords.z;
                s.Marked = t.Cuttable != null && _cache.TreeInCuttingArea(coords) ? 1 : 0;
            }
            else
            {
                s.X = t.Definition.X;
                s.Y = t.Definition.Y;
                s.Z = t.Definition.Z;
                s.Marked = 0;
            }
            s.Alive = t.Living != null && !t.Living.IsDead ? 1 : 0;
            s.Grown = t.Growable != null && t.Growable.IsGrown ? 1 : 0;
            s.Growth = t.Growable != null ? t.Growable.GrowthProgress : 0f;
        }

        private DistrictCapture[] CaptureDistrictSnapshots()
        {
            var goodIds = _cache.AllGoodIds;
            var districts = new List<DistrictCapture>();
            foreach (var dc in _districtCenterRegistry.AllDistrictCenters)
            {
                if (dc == null) continue;
                var item = new DistrictCapture { Name = dc.DistrictName };
                var pop = dc.DistrictPopulation;
                item.Adults = pop != null ? pop.NumberOfAdults : 0;
                item.Children = pop != null ? pop.NumberOfChildren : 0;
                item.Bots = pop != null ? pop.NumberOfBots : 0;
                var resources = new List<DistrictResourceCapture>();
                var counter = dc.GetComponent<DistrictResourceCounter>();
                if (counter != null)
                {
                    foreach (var goodId in goodIds)
                    {
                        var rc = counter.GetResourceCount(goodId);
                        if (rc.AllStock <= 0) continue;
                        resources.Add(new DistrictResourceCapture
                        {
                            GoodId = goodId,
                            Available = rc.AvailableStock,
                            All = rc.AllStock
                        });
                    }
                }
                item.Resources = resources.ToArray();
                districts.Add(item);
            }
            return districts.ToArray();
        }

        private DistrictSnapshot[] FinalizeDistrictSnapshots(DistrictCapture[] capture)
        {
            var districts = new DistrictSnapshot[capture.Length];
            for (int i = 0; i < capture.Length; i++)
            {
                var source = capture[i];
                var item = new DistrictSnapshot
                {
                    Name = source.Name,
                    Adults = source.Adults,
                    Children = source.Children,
                    Bots = source.Bots,
                    Resources = new Dictionary<string, (int available, int all)>()
                };
                var toonJw = new TimberbotJw(4096).BeginObj();
                var jsonJw = new TimberbotJw(4096).BeginObj();
                for (int ri = 0; ri < source.Resources.Length; ri++)
                {
                    var resource = source.Resources[ri];
                    item.Resources[resource.GoodId] = (resource.Available, resource.All);
                    toonJw.Key(resource.GoodId).Int(resource.Available);
                    jsonJw.Key(resource.GoodId).OpenObj().Prop("available", resource.Available).Prop("all", resource.All).CloseObj();
                }
                toonJw.CloseObj();
                jsonJw.CloseObj();
                item.ResourcesToon = toonJw.ToInnerString();
                item.ResourcesJson = jsonJw.ToInnerString();
                districts[i] = item;
            }
            return districts;
        }

        private void WriteClustersFiltered(TimberbotJw jw, string key,
            ProjectionSnapshot<NaturalResourceDefinition, NaturalResourceState, NoDetail>.Snapshot snapshot,
            bool treesOnly,
            int dcX, int dcY, int dcZ, int maxDist, int cellSize, int top)
        {
            _clusterCells.Clear(); _clusterSpecies.Clear();
            for (int i = 0; i < snapshot.Count; i++)
            {
                var d = snapshot.Definitions[i];
                var nr = snapshot.States[i];
                if (treesOnly) { if (d.IsTree == 0) continue; }
                else { if (d.IsGatherable == 0) continue; }
                if (nr.Alive == 0) continue;
                int cx = nr.X / cellSize * cellSize + cellSize / 2;
                int cy = nr.Y / cellSize * cellSize + cellSize / 2;
                if (nr.Z != dcZ || Math.Abs(cx - dcX) + Math.Abs(cy - dcY) > maxDist) continue;
                long k = (long)cx * 100000 + cy;
                if (!_clusterCells.ContainsKey(k))
                { _clusterCells[k] = new int[] { 0, 0, cx, cy, nr.Z }; _clusterSpecies[k] = new Dictionary<string, int>(); }
                _clusterCells[k][1]++;
                _clusterSpecies[k][d.Name] = _clusterSpecies[k].GetValueOrDefault(d.Name) + 1;
                if (nr.Grown != 0) _clusterCells[k][0]++;
            }
            _clusterSorted.Clear(); _clusterSorted.AddRange(_clusterCells.Keys);
            _clusterSorted.Sort((a, b) => _clusterCells[b][0].CompareTo(_clusterCells[a][0]));
            jw.Arr(key);
            for (int i = 0; i < Math.Min(top, _clusterSorted.Count); i++)
            {
                var s = _clusterCells[_clusterSorted[i]];
                jw.OpenObj().Prop("x", s[2]).Prop("y", s[3]).Prop("z", s[4]).Prop("grown", s[0]).Prop("total", s[1]);
                var sp = _clusterSpecies[_clusterSorted[i]];
                jw.Obj("species");
                foreach (var kv in sp) jw.Prop(kv.Key, kv.Value);
                jw.CloseObj().CloseObj();
            }
            jw.CloseArr();
        }

        // =====================================================================
        // ENTITY LIFECYCLE. add/remove tracked refs via EventBus
        // =====================================================================
        // When Timberborn creates or destroys an entity, we get an event and
        // add/remove the corresponding tracked ref. This keeps our snapshot data
        // in sync with the live game without polling.
        private void TryAddTrackedBuilding(EntityComponent ec)
        {
            if (ec.GetComponent<Building>() == null) return;
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty) return;
            if (_trackedById.ContainsKey(entityId)) return;
            int id = _cache.GetLegacyId(ec);

            var t = new TrackedBuildingRef
            {
                EntityId = entityId,
                Id = id,
                Name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name),
                BlockObject = ec.GetComponent<BlockObject>(),
                Pausable = ec.GetComponent<PausableBuilding>(),
                Floodgate = ec.GetComponent<Floodgate>(),
                BuilderPrio = ec.GetComponent<BuilderPrioritizable>(),
                Workplace = ec.GetComponent<Workplace>(),
                WorkplacePrio = ec.GetComponent<WorkplacePriority>(),
                Reachability = ec.GetComponent<EntityReachabilityStatus>(),
                Mechanical = ec.GetComponent<MechanicalBuilding>(),
                Status = ec.GetComponent<StatusSubject>(),
                PowerNode = ec.GetComponent<MechanicalNode>(),
                Site = ec.GetComponent<ConstructionSite>(),
                Inventories = ec.GetComponent<Inventories>(),
                Wonder = ec.GetComponent<Wonder>(),
                Dwelling = ec.GetComponent<Dwelling>(),
                Clutch = ec.GetComponent<Clutch>(),
                Manufactory = ec.GetComponent<Manufactory>(),
                BreedingPod = ec.GetComponent<BreedingPod>(),
                RangedEffect = ec.GetComponent<RangedEffectBuildingSpec>(),
                DistrictBuilding = ec.GetComponent<DistrictBuilding>(),
                StoragePriority = ec.GetComponent<StockpilePriority>(),
                GoodAllower = ec.GetComponent<SingleGoodAllower>(),
                Automator = ec.GetComponent<Automator>(),
                Automatable = ec.GetComponent<Automatable>(),
                DepthSensor = ec.GetComponent<DepthSensor>(),
                ContaminationSensor = ec.GetComponent<ContaminationSensor>(),
                FlowSensor = ec.GetComponent<FlowSensor>(),
                ResourceCounter = ec.GetComponent<ResourceCounter>(),
                PopulationCounter = ec.GetComponent<PopulationCounter>(),
                PowerMeter = ec.GetComponent<PowerMeter>(),
                Relay = ec.GetComponent<Relay>(),
                Memory = ec.GetComponent<Memory>(),
                Lever = ec.GetComponent<Lever>(),
                Chronometer = ec.GetComponent<Chronometer>(),
            };

            var def = new BuildingDefinition
            {
                Id = id,
                Name = t.Name,
                HasFloodgate = t.Floodgate != null ? 1 : 0,
                FloodgateMaxHeight = t.Floodgate?.MaxHeight ?? 0f,
                HasClutch = t.Clutch != null ? 1 : 0,
                HasWonder = t.Wonder != null ? 1 : 0,
                HasPowerNode = t.PowerNode != null ? 1 : 0,
                IsGenerator = t.PowerNode?.IsGenerator == true ? 1 : 0,
                IsConsumer = t.PowerNode?.IsConsumer == true ? 1 : 0,
                NominalPowerInput = t.PowerNode?._nominalPowerInput ?? 0,
                NominalPowerOutput = t.PowerNode?._nominalPowerOutput ?? 0,
                EffectRadius = t.RangedEffect?.EffectRadius ?? 0
            };

            var bo = t.BlockObject;
            if (bo != null)
            {
                var coords = bo.CoordinatesAtBaseZ;
                def.X = coords.x;
                def.Y = coords.y;
                def.Z = coords.z;
                def.Orientation = TimberbotEntityRegistry.OrientNames[(int)bo.Orientation];
                var occupied = new List<(int, int, int)>();
                try
                {
                    foreach (var block in bo.PositionedBlocks.GetAllBlocks())
                    {
                        var tc = block.Coordinates;
                        occupied.Add((tc.x, tc.y, tc.z));
                    }
                }
                catch { occupied.Add((def.X, def.Y, def.Z)); }
                def.OccupiedTiles = occupied.ToArray();
                if (bo.HasEntrance)
                {
                    try
                    {
                        var ent = bo.PositionedEntrance.DoorstepCoordinates;
                        def.HasEntrance = 1;
                        def.EntranceX = ent.x;
                        def.EntranceY = ent.y;
                    }
                    catch (Exception ex) { TimberbotLog.Error("readv2.entrance", ex); }
                }
            }

            t.Definition = def;
            _tracked.Add(t);
            _trackedById[entityId] = t;
            _snapshot.MarkDirty();
        }

        private void TryAddTrackedBeaver(EntityComponent ec)
        {
            var needMgr = ec.GetComponent<NeedManager>();
            if (needMgr == null) return;
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty || _trackedBeaversById.ContainsKey(entityId)) return;

            var t = new TrackedBeaverRef
            {
                EntityId = entityId,
                Go = ec.GameObject,
                NeedMgr = needMgr,
                WbTracker = ec.GetComponent<WellbeingTracker>(),
                Worker = ec.GetComponent<Worker>(),
                Life = ec.GetComponent<LifeProgressor>(),
                Carrier = ec.GetComponent<GoodCarrier>(),
                Deteriorable = ec.GetComponent<Deteriorable>(),
                Contaminable = ec.GetComponent<Timberborn.BeaverContaminationSystem.Contaminable>(),
                Dweller = ec.GetComponent<Dweller>(),
                Citizen = ec.GetComponent<Citizen>()
            };
            t.Definition = new BeaverDefinition
            {
                Id = _cache.GetLegacyId(ec),
                Name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name),
                IsBot = ec.GetComponent<Bot>() != null ? 1 : 0
            };
            _trackedBeavers.Add(t);
            _trackedBeaversById[entityId] = t;
            _beaverSnapshot.MarkDirty();
        }

        private void TryAddTrackedNaturalResource(EntityComponent ec)
        {
            var living = ec.GetComponent<LivingNaturalResource>();
            if (living == null) return;
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty || _trackedNaturalResourcesById.ContainsKey(entityId)) return;

            var blockObject = ec.GetComponent<BlockObject>();
            var cuttable = ec.GetComponent<Cuttable>();
            var gatherable = ec.GetComponent<Gatherable>();
            var growable = ec.GetComponent<Timberborn.Growing.Growable>();
            var coords = blockObject != null ? blockObject.Coordinates : Vector3Int.zero;
            var name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
            var definition = new NaturalResourceDefinition
            {
                Id = _cache.GetLegacyId(ec),
                Name = name,
                X = coords.x,
                Y = coords.y,
                Z = coords.z,
                IsTree = cuttable != null && TimberbotEntityRegistry.TreeSpecies.Contains(name) ? 1 : 0,
                IsCrop = cuttable != null && TimberbotEntityRegistry.CropSpecies.Contains(name) ? 1 : 0,
                IsGatherable = gatherable != null && !TimberbotEntityRegistry.TreeSpecies.Contains(name) ? 1 : 0
            };
            var tracked = new TrackedNaturalResourceRef
            {
                EntityId = entityId,
                BlockObject = blockObject,
                Cuttable = cuttable,
                Gatherable = gatherable,
                Living = living,
                Growable = growable,
                Definition = definition
            };
            _trackedNaturalResources.Add(tracked);
            _trackedNaturalResourcesById[entityId] = tracked;
            _naturalResourceSnapshot.MarkDirty();
        }

        private void RemoveTrackedBuilding(EntityComponent ec)
        {
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty) return;
            if (!_trackedById.TryGetValue(entityId, out var tracked)) return;
            _trackedById.Remove(entityId);
            _tracked.Remove(tracked);
            _snapshot.MarkDirty();
        }

        private void RemoveTrackedBeaver(EntityComponent ec)
        {
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty) return;
            if (!_trackedBeaversById.TryGetValue(entityId, out var tracked)) return;
            _trackedBeaversById.Remove(entityId);
            _trackedBeavers.Remove(tracked);
            _beaverSnapshot.MarkDirty();
        }

        private void RemoveTrackedNaturalResource(EntityComponent ec)
        {
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty) return;
            if (!_trackedNaturalResourcesById.TryGetValue(entityId, out var tracked)) return;
            _trackedNaturalResourcesById.Remove(entityId);
            _trackedNaturalResources.Remove(tracked);
            _naturalResourceSnapshot.MarkDirty();
        }

        private void TryAddTrackedBlocker(EntityComponent ec)
        {
            var blockObject = ec.GetComponent<BlockObject>();
            if (blockObject == null) return;
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty) return;
            if (_trackedBlockersById.ContainsKey(entityId)) return;
            // skip entities already tracked as building, natural resource, or beaver
            if (_trackedById.ContainsKey(entityId)) return;
            if (_trackedNaturalResourcesById.ContainsKey(entityId)) return;
            if (_trackedBeaversById.ContainsKey(entityId)) return;

            var name = TimberbotEntityRegistry.CanonicalName(ec.GameObject.name);
            var occupied = new List<(int, int, int)>();
            try
            {
                foreach (var block in blockObject.PositionedBlocks.GetAllBlocks())
                {
                    var tc = block.Coordinates;
                    occupied.Add((tc.x, tc.y, tc.z));
                }
            }
            catch { occupied.Add((blockObject.Coordinates.x, blockObject.Coordinates.y, blockObject.Coordinates.z)); }

            _trackedBlockers.Add(new TrackedBlockerRef
            {
                EntityId = entityId,
                Name = name,
                OccupiedTiles = occupied.ToArray()
            });
            _trackedBlockersById[entityId] = _trackedBlockers[_trackedBlockers.Count - 1];
        }

        private void RemoveTrackedBlocker(EntityComponent ec)
        {
            var entityId = ec.EntityId;
            if (entityId == Guid.Empty) return;
            if (!_trackedBlockersById.TryGetValue(entityId, out var tracked)) return;
            _trackedBlockersById.Remove(entityId);
            _trackedBlockers.Remove(tracked);
        }

        [OnEvent]
        public void OnEntityInitialized(EntityInitializedEvent e)
        {
            TryAddTrackedBuilding(e.Entity);
            TryAddTrackedBeaver(e.Entity);
            TryAddTrackedNaturalResource(e.Entity);
            TryAddTrackedBlocker(e.Entity);
        }

        [OnEvent]
        public void OnEntityDeleted(EntityDeletedEvent e)
        {
            RemoveTrackedBuilding(e.Entity);
            RemoveTrackedBeaver(e.Entity);
            RemoveTrackedNaturalResource(e.Entity);
            RemoveTrackedBlocker(e.Entity);
        }

        // =====================================================================
        // TRACKED REFS. live references to game entities
        // =====================================================================
        // Each tracked ref holds direct component references so we can read
        // properties (workers, wellbeing, growth) without GetComponent<T>() calls
        // every frame. Components are resolved once at entity add time.
        internal sealed class TrackedBuildingRef
        {
            public Guid EntityId;
            public int Id;
            public string Name;
            public BlockObject BlockObject;
            public PausableBuilding Pausable;
            public Floodgate Floodgate;
            public BuilderPrioritizable BuilderPrio;
            public Workplace Workplace;
            public WorkplacePriority WorkplacePrio;
            public EntityReachabilityStatus Reachability;
            public MechanicalBuilding Mechanical;
            public StatusSubject Status;
            public MechanicalNode PowerNode;
            public ConstructionSite Site;
            public Inventories Inventories;
            public Wonder Wonder;
            public Dwelling Dwelling;
            public Clutch Clutch;
            public Manufactory Manufactory;
            public BreedingPod BreedingPod;
            public RangedEffectBuildingSpec RangedEffect;
            public DistrictBuilding DistrictBuilding;
            public StockpilePriority StoragePriority;
            public SingleGoodAllower GoodAllower;
            public BuildingDefinition Definition;
            public Automator Automator;
            public Automatable Automatable;
            public DepthSensor DepthSensor;
            public ContaminationSensor ContaminationSensor;
            public FlowSensor FlowSensor;
            public ResourceCounter ResourceCounter;
            public PopulationCounter PopulationCounter;
            public PowerMeter PowerMeter;
            public Relay Relay;
            public Memory Memory;
            public Lever Lever;
            public Chronometer Chronometer;
        }

        internal sealed class TrackedBeaverRef
        {
            public Guid EntityId;
            public GameObject Go;
            public NeedManager NeedMgr;
            public WellbeingTracker WbTracker;
            public Worker Worker;
            public LifeProgressor Life;
            public GoodCarrier Carrier;
            public Deteriorable Deteriorable;
            public Timberborn.BeaverContaminationSystem.Contaminable Contaminable;
            public Dweller Dweller;
            public Citizen Citizen;
            public BeaverDefinition Definition;
        }

        internal sealed class TrackedNaturalResourceRef
        {
            public Guid EntityId;
            public BlockObject BlockObject;
            public Cuttable Cuttable;
            public Gatherable Gatherable;
            public LivingNaturalResource Living;
            public Timberborn.Growing.Growable Growable;
            public NaturalResourceDefinition Definition;
        }

        internal sealed class TrackedBlockerRef
        {
            public Guid EntityId;
            public string Name;
            public (int x, int y, int z)[] OccupiedTiles;
        }

        // =====================================================================
        // DTO STRUCTS. plain data objects that live in snapshot buffers
        // =====================================================================
        // These hold the actual data served to HTTP clients. Definition = static
        // (set once), State = mutable (refreshed each snapshot), Detail = expensive
        // (only populated on full-detail requests like ?detail=full).
        internal sealed class BuildingDefinition
        {
            public int Id;
            public string Name;
            public int X, Y, Z;
            public string Orientation;
            public int HasPowerNode;
            public int HasFloodgate;
            public float FloodgateMaxHeight;
            public int HasClutch;
            public int HasWonder;
            public int IsGenerator, IsConsumer;
            public int NominalPowerInput, NominalPowerOutput;
            public int EffectRadius;
            public (int x, int y, int z)[] OccupiedTiles;
            public int HasEntrance;
            public int EntranceX, EntranceY;
        }

        internal sealed class BuildingState
        {
            public int Finished, Pausable, Paused, Unreachable, Reachable, Powered;
            public string District;
            public int AssignedWorkers, DesiredWorkers, MaxWorkers;
            public int Dwellers, MaxDwellers;
            public float FloodgateHeight;
            public string ConstructionPriority, WorkplacePriorityStr;
            public float BuildProgress, MaterialProgress;
            public int HasMaterials;
            public int ClutchEngaged, WonderActive;
            public int PowerDemand, PowerSupply, PowerNetworkId;
            public string CurrentRecipe;
            public float ProductionProgress;
            public int ReadyToProduce;
            public int NeedsNutrients, Nutrients;
            public int Stock, Capacity;
            public string StorageMode, AllowedGood;
            public string[] StatusAlerts;
            public int HasAutomator, IsTransmitter;
            public string AutomatorState;
            public int IsAutomated;
            public string InputName;
            public string AutomatorName;
            public string AutomationType;
            public string AutomationConfig;
            public string AutomationInputsJson;
            public string AutomationOutputsJson;
        }

        internal sealed class BuildingDetailState
        {
            public readonly Dictionary<string, int> Inventory = new Dictionary<string, int>();
            public string InventoryToon = "";
            public readonly List<string> Recipes = new List<string>();
            public string RecipesToon = "";
            public readonly Dictionary<string, int> NutrientStock = new Dictionary<string, int>();
        }

        internal sealed class BeaverDefinition
        {
            public int Id;
            public string Name;
            public int IsBot;
        }

        internal sealed class BeaverState
        {
            public float Wellbeing;
            public int X, Y, Z;
            public string Workplace, District;
            public int HasHome, Contaminated;
            public float LifeProgress, DeteriorationProgress;
            public int IsCarrying;
            public string CarryingGood;
            public int CarryAmount, LiftingCapacity;
            public int Overburdened;
            public int AnyCritical;
            public string Critical, Unmet;
        }

        internal sealed class BeaverNeed
        {
            public string Id, Group;
            public float Points;
            public int Wellbeing;
            public int Favorable, Critical, Active;
        }

        internal sealed class BeaverDetailState
        {
            public readonly List<BeaverNeed> Needs = new List<BeaverNeed>();
        }

        internal sealed class NaturalResourceDefinition
        {
            public int Id;
            public string Name;
            public int X, Y, Z;
            public int IsTree, IsCrop, IsGatherable;
        }

        internal sealed class NaturalResourceState
        {
            public int X, Y, Z;
            public int Marked, Alive, Grown;
            public float Growth;
        }

        internal sealed class NoDetail { }

        internal sealed class DistrictSnapshot
        {
            public string Name;
            public int Adults, Children, Bots;
            public Dictionary<string, (int available, int all)> Resources;
            public string ResourcesToon;
            public string ResourcesJson;
        }

        internal interface ICollectionSchema<TDef, TState, TDetail>
        {
            int GetId(TDef def);
            string GetName(TDef def);
            int GetX(TDef def, TState state);
            int GetY(TDef def, TState state);
            bool IncludeRow(TDef def, TState state);
            void WriteRow(TimberbotJw jw, string format, bool fullDetail, TDef def, TState state, TDetail detail);
        }

        // =====================================================================
        // GENERIC SNAPSHOT PIPELINE
        // =====================================================================
        // ProjectionSnapshot manages the full lifecycle of an entity collection:
        //   1. HTTP reader calls RequestFresh() -> sets flag, blocks on ManualResetEvent
        //   2. Main thread ProcessPendingCapture() -> reads live state into buffer arrays
        //   3. Finalize thread publishes immutable Snapshot -> wakes all waiting readers
        //
        // Double-buffered: two Buffer objects (A and B) alternate. While one is being
        // captured, the other holds the last published snapshot for readers.
        //
        // Budget-aware: capture can pause mid-array and resume next frame. The main
        // thread tracks _captureIndex so it picks up where it left off.
        //
        // TDef  = static definition (id, name, coords). set once at entity add
        // TState = mutable state (workers, wellbeing). refreshed every snapshot
        // TDetail = expensive detail (inventory strings). only on full-detail requests
        internal sealed class ProjectionSnapshot<TDef, TState, TDetail>
            where TDef : class
            where TState : class, new()
            where TDetail : class, new()
        {
            public delegate TDef GetDefinition(int index);
            public delegate void RefreshState(TState state, int index);
            public delegate void RefreshDetail(TDetail detail, int index);
            public delegate void FinalizeBuffer(Buffer buffer, int count, bool fullDetail);

            private readonly object _lock = new object();
            private bool _refreshRequested;
            private bool _fullRequested;
            private readonly List<Waiter> _waiters = new List<Waiter>();
            private readonly Buffer _bufA = new Buffer();
            private readonly Buffer _bufB = new Buffer();
            private Buffer _writeBuf;
            private Snapshot _published = Snapshot.Empty;

            private int _sequence;
            private int _writeBarrier;
            private bool _captureInProgress;
            private bool _finalizeInProgress;
            private Buffer _captureBuf;
            private int _captureCount;
            private int _captureIndex;
            private float _capturePublishedAt;
            private bool _captureFullDetail;
            private readonly List<Waiter> _activeWaiters = new List<Waiter>();
            private double _lastCaptureMs;
            private double _lastFinalizeMs;
            private readonly ICollectionSchema<TDef, TState, TDetail> _schema;

            internal ProjectionSnapshot(ICollectionSchema<TDef, TState, TDetail> schema)
            {
                _writeBuf = _bufA;
                _schema = schema;
            }

            public int Sequence => _sequence;
            public int Count => _published.Count;
            public float PublishedAt => _published.PublishedAt;
            public Snapshot Current => _published;
            public double LastCaptureMs => _lastCaptureMs;
            public double LastFinalizeMs => _lastFinalizeMs;
            public int PendingWaiterCount { get { lock (_lock) return _waiters.Count + _activeWaiters.Count; } }
            public bool InFlight { get { lock (_lock) return _captureInProgress || _finalizeInProgress; } }
            public void MarkDirty() { _bufA.StructureDirty = true; _bufB.StructureDirty = true; }

            public void Invalidate()
            {
                lock (_lock)
                {
                    _refreshRequested = true;
                    _writeBarrier = _sequence + (_captureInProgress || _finalizeInProgress ? 1 : 0);
                }
            }

            public void ProcessPendingCapture(
                float now,
                int count,
                GetDefinition getDef,
                RefreshState refreshState,
                RefreshDetail refreshDetail,
                FinalizeBuffer finalizeBuffer,
                Action<Action> enqueueFinalize,
                Func<bool> budgetExceeded)
            {
                Buffer captureBuf;
                int captureCount;
                int startIndex;
                bool fullDetail;
                bool startingCapture = false;
                lock (_lock)
                {
                    if (_finalizeInProgress) return;
                    if (!_captureInProgress)
                    {
                        if (!_refreshRequested) return;
                        _refreshRequested = false;
                        _captureInProgress = true;
                        _captureBuf = _writeBuf;
                        _captureCount = count;
                        _captureIndex = 0;
                        _capturePublishedAt = now;
                        _captureFullDetail = _fullRequested;
                        _fullRequested = false;
                        _activeWaiters.Clear();
                        _activeWaiters.AddRange(_waiters);
                        _waiters.Clear();
                        startingCapture = true;
                    }
                    captureBuf = _captureBuf;
                    captureCount = _captureCount;
                    startIndex = _captureIndex;
                    fullDetail = _captureFullDetail;
                }

                int i = startIndex;
                try
                {
                    var sw = Stopwatch.StartNew();
                    if (startingCapture)
                    {
                        captureBuf.EnsureCapacity(captureCount);
                        for (int di = 0; di < captureCount; di++)
                            captureBuf.Definitions[di] = getDef(di);
                    }

                    for (; i < captureCount; i++)
                    {
                        refreshState(captureBuf.States[i], i);
                        if (fullDetail)
                            refreshDetail?.Invoke(captureBuf.Details[i], i);
                        if (budgetExceeded())
                        {
                            i++;
                            break;
                        }
                    }

                    lock (_lock)
                    {
                        _lastCaptureMs = sw.Elapsed.TotalMilliseconds;
                        if (i < captureCount)
                        {
                            _captureIndex = i;
                            return;
                        }
                        _captureInProgress = false;
                        _finalizeInProgress = true;
                        _writeBuf = ReferenceEquals(captureBuf, _bufA) ? _bufB : _bufA;
                    }
                }
                catch (Exception ex)
                {
                    List<Waiter> wakeBatch;
                    lock (_lock)
                    {
                        _captureInProgress = false;
                        _finalizeInProgress = false;
                        _refreshRequested = false;
                        _fullRequested = false;
                        wakeBatch = new List<Waiter>(_activeWaiters);
                        wakeBatch.AddRange(_waiters);
                        _activeWaiters.Clear();
                        _waiters.Clear();
                    }
                    for (int wi = 0; wi < wakeBatch.Count; wi++)
                        wakeBatch[wi].Signal.Set();
                    TimberbotLog.Error("readv2.capture", ex);
                    return;
                }

                enqueueFinalize(() =>
                {
                    var finalizeSw = Stopwatch.StartNew();
                    SortBuffer(captureBuf, captureCount);
                    finalizeBuffer?.Invoke(captureBuf, captureCount, fullDetail);
                    List<Waiter> wakeBatch;
                    lock (_lock)
                    {
                        _published = new Snapshot
                        {
                            Definitions = captureBuf.Definitions,
                            States = captureBuf.States,
                            Details = fullDetail ? captureBuf.Details : null,
                            Count = captureCount,
                            PublishedAt = _capturePublishedAt
                        };
                        _sequence++;
                        _lastFinalizeMs = finalizeSw.Elapsed.TotalMilliseconds;
                        _finalizeInProgress = false;
                        wakeBatch = new List<Waiter>(_activeWaiters);
                        _activeWaiters.Clear();
                    }
                    for (int wi = 0; wi < wakeBatch.Count; wi++)
                        wakeBatch[wi].Signal.Set();
                });
            }

            private void SortBuffer(Buffer buf, int count)
            {
                if (count <= 1) return;
                var indices = new int[count];
                for (int i = 0; i < count; i++) indices[i] = i;
                Array.Sort(indices, (a, b) => _schema.GetId(buf.Definitions[a]).CompareTo(_schema.GetId(buf.Definitions[b])));

                var newDefs = new TDef[buf.Definitions.Length];
                var newStates = new TState[buf.States.Length];
                var newDetails = new TDetail[buf.Details.Length];
                for (int i = 0; i < count; i++)
                {
                    int oldIdx = indices[i];
                    newDefs[i] = buf.Definitions[oldIdx];
                    newStates[i] = buf.States[oldIdx];
                    newDetails[i] = buf.Details[oldIdx];
                }
                for (int i = count; i < buf.Definitions.Length; i++)
                {
                    newStates[i] = buf.States[i];
                    newDetails[i] = buf.Details[i];
                }
                buf.Definitions = newDefs;
                buf.States = newStates;
                buf.Details = newDetails;
            }

            public Snapshot RequestFresh(bool fullDetail, int timeoutMs)
            {
                int targetSeq;
                lock (_lock)
                {
                    _refreshRequested = true;
                    if (fullDetail) _fullRequested = true;
                    targetSeq = Math.Max(_sequence + 1, _writeBarrier + 1);
                }

                var sw = Stopwatch.StartNew();
                while (true)
                {
                    var waiter = new Waiter();
                    lock (_lock)
                    {
                        if (_sequence >= targetSeq) return _published;
                        _waiters.Add(waiter);
                    }
                    if (!waiter.Signal.Wait(Math.Max(0, timeoutMs - (int)sw.ElapsedMilliseconds)))
                    {
                        lock (_lock) _waiters.Remove(waiter);
                        throw new TimeoutException();
                    }
                    if (_sequence >= targetSeq) return _published;
                    if (sw.ElapsedMilliseconds >= timeoutMs) throw new TimeoutException();
                }
            }
            public Snapshot PublishNow(float now, int count, GetDefinition getDef, RefreshState refreshState, RefreshDetail refreshDetail = null, FinalizeBuffer finalizeBuffer = null)
            {
                Publish(count, getDef, refreshState, refreshDetail, now, finalizeBuffer);
                return _published;
            }

            private void Publish(int count, GetDefinition getDef, RefreshState refreshState, RefreshDetail refreshDetail, float now, FinalizeBuffer finalizeBuffer)
            {
                var buf = _writeBuf;
                var captureSw = Stopwatch.StartNew();
                buf.EnsureCapacity(count);
                for (int i = 0; i < count; i++)
                    buf.Definitions[i] = getDef(i);

                for (int i = 0; i < count; i++)
                {
                    refreshState(buf.States[i], i);
                    refreshDetail?.Invoke(buf.Details[i], i);
                }
                _lastCaptureMs = captureSw.Elapsed.TotalMilliseconds;
                var finalizeSw = Stopwatch.StartNew();
                finalizeBuffer?.Invoke(buf, count, refreshDetail != null);
                _lastFinalizeMs = finalizeSw.Elapsed.TotalMilliseconds;

                _published = new Snapshot
                {
                    Definitions = buf.Definitions,
                    States = buf.States,
                    Details = refreshDetail != null ? buf.Details : null,
                    Count = count,
                    PublishedAt = now
                };
                _sequence++;
                _writeBuf = ReferenceEquals(buf, _bufA) ? _bufB : _bufA;
            }

            public sealed class Snapshot
            {
                public static readonly Snapshot Empty = new Snapshot
                {
                    Definitions = Array.Empty<TDef>(),
                    States = Array.Empty<TState>(),
                    Details = null,
                    Count = 0,
                    PublishedAt = 0f
                };

                public TDef[] Definitions;
                public TState[] States;
                public TDetail[] Details;
                public int Count;
                public float PublishedAt;
            }

            private sealed class Waiter
            {
                public readonly ManualResetEventSlim Signal = new ManualResetEventSlim(false);
            }

            internal sealed class Buffer
            {
                public TDef[] Definitions = Array.Empty<TDef>();
                public TState[] States = Array.Empty<TState>();
                public TDetail[] Details = Array.Empty<TDetail>();
                public int Length;
                public bool StructureDirty = true;

                public void EnsureCapacity(int count)
                {
                    if (count <= Definitions.Length) { Length = count; return; }
                    int capacity = Math.Max(count, Math.Max(32, Definitions.Length * 2));
                    var newDefs = new TDef[capacity];
                    var newStates = new TState[capacity];
                    var newDetails = new TDetail[capacity];
                    int oldLen = Definitions.Length;
                    if (oldLen > 0)
                    {
                        Array.Copy(Definitions, newDefs, oldLen);
                        Array.Copy(States, newStates, oldLen);
                        Array.Copy(Details, newDetails, oldLen);
                    }
                    for (int i = oldLen; i < capacity; i++)
                    {
                        newStates[i] = new TState();
                        newDetails[i] = new TDetail();
                    }
                    Definitions = newDefs;
                    States = newStates;
                    Details = newDetails;
                    Length = count;
                }
            }
        }

        // CollectionRoute: HTTP endpoint helper for entity collections.
        // Handles the full request cycle: request fresh snapshot, wait for publish,
        // then filter/paginate/serialize from the published data. Supports both
        // toon (compact CSV for AI) and json (nested objects) output formats.
        private sealed class CollectionRoute<TDef, TState, TDetail>
            where TDef : class
            where TState : class, new()
            where TDetail : class, new()
        {
            private readonly TimberbotJw _jw;
            private readonly Func<bool, int, ProjectionSnapshot<TDef, TState, TDetail>.Snapshot> _snapshotProvider;
            private readonly ICollectionSchema<TDef, TState, TDetail> _schema;

            public CollectionRoute(
                TimberbotJw jw,
                Func<bool, int, ProjectionSnapshot<TDef, TState, TDetail>.Snapshot> snapshotProvider,
                ICollectionSchema<TDef, TState, TDetail> schema)
            {
                _jw = jw;
                _snapshotProvider = snapshotProvider;
                _schema = schema;
            }

            public object Collect(string format, string detail, int id, int limit, int offset, string filterName, int filterX, int filterY, int filterRadius)
            {
                var query = PureCollectionQuery.Parse(format, detail, id, limit, offset, filterName, filterX, filterY, filterRadius);
                ProjectionSnapshot<TDef, TState, TDetail>.Snapshot snapshot;
                try { snapshot = _snapshotProvider(query.NeedsFullDetail, 2000); }
                catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }

                int total = snapshot.Count;
                if (query.Paginated && query.HasFilter)
                {
                    total = 0;
                    for (int i = 0; i < snapshot.Count; i++)
                        if (ShouldInclude(query, snapshot.Definitions[i], snapshot.States[i])) total++;
                }

                int skipped = 0, emitted = 0;
                var jw = _jw.Reset();
                if (query.Paginated)
                    jw.OpenObj().Prop("total", total).Prop("offset", query.Offset).Prop("limit", query.Limit).Key("items");
                jw.OpenArr();
                for (int i = 0; i < snapshot.Count; i++)
                {
                    var def = snapshot.Definitions[i];
                    var state = snapshot.States[i];
                    if (!ShouldInclude(query, def, state)) continue;
                    if (query.Offset > 0 && skipped < query.Offset) { skipped++; continue; }
                    if (query.Paginated && emitted >= query.Limit) break;
                    emitted++;
                    var detailState = query.NeedsFullDetail && snapshot.Details != null ? snapshot.Details[i] : null;
                    _schema.WriteRow(jw, query.Format, query.NeedsFullDetail, def, state, detailState);
                }
                jw.CloseArr();
                if (query.Paginated) jw.CloseObj();
                return jw.ToString();
            }

            private bool ShouldInclude(PureCollectionQuery query, TDef def, TState state)
            {
                if (!_schema.IncludeRow(def, state)) return false;
                if (query.SingleId.HasValue && _schema.GetId(def) != query.SingleId.Value) return false;
                return TimberbotPure.PassesFilter(_schema.GetName(def), _schema.GetX(def, state), _schema.GetY(def, state), query.FilterName, query.FilterX, query.FilterY, query.FilterRadius);
            }
        }

        private sealed class BuildingCollectionSchema : ICollectionSchema<BuildingDefinition, BuildingState, BuildingDetailState>
        {
            public int GetId(BuildingDefinition def) => def.Id;
            public string GetName(BuildingDefinition def) => def.Name;
            public int GetX(BuildingDefinition def, BuildingState state) => def.X;
            public int GetY(BuildingDefinition def, BuildingState state) => def.Y;
            public bool IncludeRow(BuildingDefinition def, BuildingState state) => true;

            public void WriteRow(TimberbotJw jw, string format, bool fullDetail, BuildingDefinition d, BuildingState s, BuildingDetailState detailState)
            {
                jw.OpenObj()
                    .Prop("id", d.Id)
                    .Prop("name", d.Name)
                    .Prop("x", d.X).Prop("y", d.Y).Prop("z", d.Z)
                    .Prop("orientation", d.Orientation ?? "")
                    .Prop("finished", s.Finished)
                    .Prop("paused", s.Paused);

                if (!fullDetail)
                {
                    jw.Prop("priority", s.ConstructionPriority ?? "")
                        .Prop("workers", s.MaxWorkers > 0 ? $"{s.AssignedWorkers}/{s.DesiredWorkers}" : "")
                        .Prop("alerts", s.StatusAlerts != null && s.StatusAlerts.Length > 0 ? string.Join(",", s.StatusAlerts) : "")
                        .CloseObj();
                    return;
                }

                jw.Prop("constructionPriority", s.ConstructionPriority ?? "")
                    .Prop("pausable", s.Pausable)
                    .Prop("workplacePriority", s.WorkplacePriorityStr ?? "")
                    .Prop("maxWorkers", s.MaxWorkers)
                    .Prop("desiredWorkers", s.DesiredWorkers)
                    .Prop("assignedWorkers", s.AssignedWorkers)
                    .Prop("reachable", s.Reachable)
                    .Prop("powered", s.Powered)
                    .Prop("isGenerator", d.IsGenerator)
                    .Prop("isConsumer", d.IsConsumer)
                    .Prop("nominalPowerInput", d.NominalPowerInput)
                    .Prop("nominalPowerOutput", d.NominalPowerOutput)
                    .Prop("powerDemand", s.PowerDemand)
                    .Prop("powerSupply", s.PowerSupply)
                    .Prop("buildProgress", s.BuildProgress)
                    .Prop("materialProgress", s.MaterialProgress)
                    .Prop("hasMaterials", s.HasMaterials)
                    .Prop("stock", s.Stock)
                    .Prop("capacity", s.Capacity)
                    .Prop("storageMode", s.StorageMode)
                    .Prop("allowedGood", s.AllowedGood)
                    .Prop("dwellers", s.Dwellers)
                    .Prop("maxDwellers", s.MaxDwellers)
                    .Prop("floodgate", d.HasFloodgate)
                    .Prop("height", d.HasFloodgate != 0 ? s.FloodgateHeight : 0f, "F1")
                    .Prop("maxHeight", d.HasFloodgate != 0 ? d.FloodgateMaxHeight : 0f, "F1")
                    .Prop("isClutch", d.HasClutch)
                    .Prop("clutchEngaged", s.ClutchEngaged)
                    .Prop("currentRecipe", s.CurrentRecipe ?? "")
                    .Prop("productionProgress", s.ProductionProgress)
                    .Prop("readyToProduce", s.ReadyToProduce)
                    .Prop("needsNutrients", s.NeedsNutrients)
                    .Prop("nutrients", s.Nutrients)
                    .Prop("effectRadius", d.EffectRadius)
                    .Prop("isWonder", d.HasWonder)
                    .Prop("wonderActive", s.WonderActive)
                    .Prop("alerts", s.StatusAlerts != null && s.StatusAlerts.Length > 0 ? string.Join(",", s.StatusAlerts) : "")
                    .Obj("automation")
                        .Prop("hasAutomator", s.HasAutomator)
                        .Prop("isTransmitter", s.IsTransmitter)
                        .Prop("state", s.AutomatorState ?? "")
                        .Prop("isAutomated", s.IsAutomated)
                        .Prop("inputName", s.InputName ?? "")
                        .Prop("automatorName", s.AutomatorName ?? "")
                        .Prop("type", s.AutomationType ?? "")
                        .RawProp("config", string.IsNullOrEmpty(s.AutomationConfig) ? "{}" : s.AutomationConfig)
                        .RawProp("inputs", string.IsNullOrEmpty(s.AutomationInputsJson) ? "[]" : s.AutomationInputsJson)
                        .RawProp("outputs", string.IsNullOrEmpty(s.AutomationOutputsJson) ? "[]" : s.AutomationOutputsJson)
                    .CloseObj();

                if (format == "toon")
                {
                    jw.Prop("inventory", detailState?.InventoryToon ?? "")
                        .Prop("recipes", detailState?.RecipesToon ?? "");
                }
                else
                {
                    jw.Obj("inventory");
                    if (detailState?.Inventory != null)
                        foreach (var kvp in detailState.Inventory)
                            jw.Key(kvp.Key).Int(kvp.Value);
                    jw.CloseObj();
                    jw.Arr("recipes");
                    if (detailState?.Recipes != null)
                        for (int ri = 0; ri < detailState.Recipes.Count; ri++)
                            jw.Str(detailState.Recipes[ri]);
                    jw.CloseArr();
                }
                jw.CloseObj();
            }
        }

        private sealed class BeaverCollectionSchema : ICollectionSchema<BeaverDefinition, BeaverState, BeaverDetailState>
        {
            public int GetId(BeaverDefinition def) => def.Id;
            public string GetName(BeaverDefinition def) => def.Name;
            public int GetX(BeaverDefinition def, BeaverState state) => state.X;
            public int GetY(BeaverDefinition def, BeaverState state) => state.Y;
            public bool IncludeRow(BeaverDefinition def, BeaverState state) => true;

            public void WriteRow(TimberbotJw jw, string format, bool fullDetail, BeaverDefinition d, BeaverState s, BeaverDetailState detail)
            {
                jw.OpenObj()
                    .Prop("id", d.Id)
                    .Prop("name", d.Name)
                    .Prop("x", s.X).Prop("y", s.Y).Prop("z", s.Z)
                    .Prop("wellbeing", s.Wellbeing, "F1")
                    .Prop("isBot", d.IsBot);

                if (!fullDetail)
                {
                    jw.Prop("tier", TimberbotPure.GetBeaverTier(s.Wellbeing, d.IsBot != 0))
                        .Prop("workplace", s.Workplace ?? "")
                        .Prop("critical", s.Critical ?? "")
                        .Prop("unmet", s.Unmet ?? "")
                        .CloseObj();
                    return;
                }

                jw.Prop("anyCritical", s.AnyCritical)
                    .Prop("workplace", s.Workplace ?? "")
                    .Prop("district", s.District ?? "")
                    .Prop("hasHome", s.HasHome)
                    .Prop("contaminated", s.Contaminated)
                    .Prop("lifeProgress", s.LifeProgress)
                    .Prop("deterioration", s.DeteriorationProgress, "F3")
                    .Prop("liftingCapacity", s.LiftingCapacity)
                    .Prop("overburdened", s.Overburdened)
                    .Prop("carrying", s.IsCarrying != 0 ? s.CarryingGood : "")
                    .Prop("carryAmount", s.IsCarrying != 0 ? s.CarryAmount : 0);
                jw.Arr("needs");
                if (detail != null)
                {
                    foreach (var n in detail.Needs)
                    {
                        jw.OpenObj()
                            .Prop("id", n.Id)
                            .Prop("points", n.Points)
                            .Prop("wellbeing", n.Wellbeing)
                            .Prop("favorable", n.Favorable)
                            .Prop("critical", n.Critical)
                            .Prop("group", n.Group)
                            .CloseObj();
                    }
                }
                jw.CloseArr().CloseObj();
            }
        }

        private sealed class TreeCollectionSchema : ICollectionSchema<NaturalResourceDefinition, NaturalResourceState, NoDetail>
        {
            public int GetId(NaturalResourceDefinition def) => def.Id;
            public string GetName(NaturalResourceDefinition def) => def.Name;
            public int GetX(NaturalResourceDefinition def, NaturalResourceState state) => state.X;
            public int GetY(NaturalResourceDefinition def, NaturalResourceState state) => state.Y;
            public bool IncludeRow(NaturalResourceDefinition def, NaturalResourceState state) => def.IsTree != 0;
            public void WriteRow(TimberbotJw jw, string format, bool fullDetail, NaturalResourceDefinition d, NaturalResourceState s, NoDetail detail)
                => WriteNaturalRow(jw, d, s);
        }

        private sealed class CropCollectionSchema : ICollectionSchema<NaturalResourceDefinition, NaturalResourceState, NoDetail>
        {
            public int GetId(NaturalResourceDefinition def) => def.Id;
            public string GetName(NaturalResourceDefinition def) => def.Name;
            public int GetX(NaturalResourceDefinition def, NaturalResourceState state) => state.X;
            public int GetY(NaturalResourceDefinition def, NaturalResourceState state) => state.Y;
            public bool IncludeRow(NaturalResourceDefinition def, NaturalResourceState state) => def.IsCrop != 0;
            public void WriteRow(TimberbotJw jw, string format, bool fullDetail, NaturalResourceDefinition d, NaturalResourceState s, NoDetail detail)
                => WriteNaturalRow(jw, d, s);
        }

        private sealed class GatherableCollectionSchema : ICollectionSchema<NaturalResourceDefinition, NaturalResourceState, NoDetail>
        {
            public int GetId(NaturalResourceDefinition def) => def.Id;
            public string GetName(NaturalResourceDefinition def) => def.Name;
            public int GetX(NaturalResourceDefinition def, NaturalResourceState state) => state.X;
            public int GetY(NaturalResourceDefinition def, NaturalResourceState state) => state.Y;
            public bool IncludeRow(NaturalResourceDefinition def, NaturalResourceState state) => def.IsGatherable != 0;

            public void WriteRow(TimberbotJw jw, string format, bool fullDetail, NaturalResourceDefinition d, NaturalResourceState s, NoDetail detail)
                => jw.OpenObj()
                    .Prop("id", d.Id)
                    .Prop("name", d.Name)
                    .Prop("x", s.X).Prop("y", s.Y).Prop("z", s.Z)
                    .Prop("alive", s.Alive)
                    .CloseObj();
        }

        private static void WriteNaturalRow(TimberbotJw jw, NaturalResourceDefinition d, NaturalResourceState s)
        {
            jw.OpenObj()
                .Prop("id", d.Id)
                .Prop("name", d.Name)
                .Prop("x", s.X).Prop("y", s.Y).Prop("z", s.Z)
                .Prop("marked", s.Marked)
                .Prop("alive", s.Alive)
                .Prop("grown", s.Grown)
                .Prop("growth", s.Growth)
                .CloseObj();
        }

        private sealed class SettlementSnapshot { public string Name; }
        private sealed class TimeSnapshot { public int DayNumber; public float DayProgress; public float PartialDayNumber; }
        private sealed class WeatherSnapshot
        {
            public int Cycle;
            public int CycleDay;
            public bool IsHazardous;
            public int TemperateWeatherDuration;
            public int HazardousWeatherDuration;
            public int CycleLengthInDays;
        }
        private sealed class SpeedSnapshot { public int Speed; }
        private sealed class WorkHoursSnapshot { public float EndHours; public bool AreWorkingHours; }
        private sealed class ScienceCapture
        {
            public int Points;
            public ScienceUnlockableCapture[] Unlockables;
        }
        private sealed class ScienceUnlockableCapture
        {
            public string Name;
            public int Cost;
            public int Unlocked;
        }
        private sealed class DistributionCapture
        {
            public DistributionDistrictCapture[] Districts;
        }
        private sealed class DistributionDistrictCapture
        {
            public string District;
            public DistributionGoodCapture[] Goods;
        }
        private sealed class DistributionGoodCapture
        {
            public string Good;
            public string ImportOption;
            public float ExportThreshold;
        }
        private sealed class DistrictCapture
        {
            public string Name;
            public int Adults, Children, Bots;
            public DistrictResourceCapture[] Resources;
        }
        private sealed class DistrictResourceCapture
        {
            public string GoodId;
            public int Available;
            public int All;
        }
        private sealed class RawJsonSnapshot { public string Json; }
        private sealed class NotificationItem { public string Subject; public string Description; public int Cycle; public int CycleDay; }
        private sealed class AlertItem { public string Type; public int Id; public string Name; public string Workers; }
        private sealed class PowerBuildingItem { public string Name; public int Id; public int IsGenerator; public int NominalOutput; public int NominalInput; }
        private sealed class PowerNetworkItem { public int Id; public int Supply; public int Demand; public PowerBuildingItem[] Buildings; }
        private sealed class PowerNetworkBuilder { public int Id; public int Supply; public int Demand; public List<PowerBuildingItem> Buildings; }

        private interface IValueSchema<TSnapshot>
        {
            void Write(TimberbotJw jw, string format, TSnapshot snapshot);
        }

        // ValueStore: same fresh-on-request pattern as ProjectionSnapshot, but for
        // singleton/aggregate endpoints (time, weather, speed, science, distribution).
        // Capture produces a typed payload on the main thread, finalize may transform
        // it (e.g. pre-build JSON), publish makes it available to readers.
        private sealed class ValueStore<TCapture, TSnapshot>
            where TCapture : class
            where TSnapshot : class
        {
            public delegate TCapture CaptureSnapshot();
            public delegate TSnapshot FinalizeSnapshot(TCapture capture);

            private readonly object _lock = new object();
            private bool _refreshRequested;
            private readonly List<Waiter> _waiters = new List<Waiter>();
            private readonly List<Waiter> _activeWaiters = new List<Waiter>();
            private TSnapshot _published;
            private int _sequence;
            private float _publishedAt;
            private bool _finalizeInProgress;
            private double _lastCaptureMs;
            private double _lastFinalizeMs;
            public TSnapshot Current => _published;
            public int Sequence => _sequence;
            public float PublishedAt => _publishedAt;
            public double LastCaptureMs => _lastCaptureMs;
            public double LastFinalizeMs => _lastFinalizeMs;
            public int PendingWaiterCount { get { lock (_lock) return _waiters.Count + _activeWaiters.Count; } }
            public bool InFlight { get { lock (_lock) return _finalizeInProgress; } }
            public int Count
            {
                get
                {
                    if (_published == null) return 0;
                    if (_published is Array arr) return arr.Length;
                    return 1;
                }
            }

            public void ProcessPendingCapture(float now, CaptureSnapshot capture, FinalizeSnapshot finalize, Action<Action> enqueueFinalize, Func<bool> budgetExceeded)
            {
                if (budgetExceeded()) return;
                List<Waiter> activeWaiters;
                TCapture captured;
                lock (_lock)
                {
                    if (!_refreshRequested || _finalizeInProgress) return;
                    _refreshRequested = false;
                    _activeWaiters.Clear();
                    _activeWaiters.AddRange(_waiters);
                    _waiters.Clear();
                    _finalizeInProgress = true;
                    activeWaiters = new List<Waiter>(_activeWaiters);
                }

                try
                {
                    var captureSw = Stopwatch.StartNew();
                    captured = capture();
                    _lastCaptureMs = captureSw.Elapsed.TotalMilliseconds;
                }
                catch (Exception ex)
                {
                    lock (_lock)
                    {
                        _finalizeInProgress = false;
                        _activeWaiters.Clear();
                    }
                    for (int i = 0; i < activeWaiters.Count; i++)
                        activeWaiters[i].Signal.Set();
                    TimberbotLog.Error("readv2.value_capture", ex);
                    return;
                }
                enqueueFinalize(() =>
                {
                    var finalizeSw = Stopwatch.StartNew();
                    var published = finalize(captured);
                    lock (_lock)
                    {
                        _published = published;
                        _publishedAt = now;
                        _sequence++;
                        _lastFinalizeMs = finalizeSw.Elapsed.TotalMilliseconds;
                        _finalizeInProgress = false;
                        _activeWaiters.Clear();
                    }
                    for (int i = 0; i < activeWaiters.Count; i++)
                        activeWaiters[i].Signal.Set();
                });
            }

            public TSnapshot RequestFresh(int timeoutMs)
            {
                var waiter = new Waiter();
                lock (_lock)
                {
                    _refreshRequested = true;
                    _waiters.Add(waiter);
                }
                if (!waiter.Signal.Wait(timeoutMs))
                {
                    lock (_lock) _waiters.Remove(waiter);
                    throw new TimeoutException();
                }
                return _published;
            }

            public TSnapshot PublishNow(float now, CaptureSnapshot capture, FinalizeSnapshot finalize)
            {
                var captureSw = Stopwatch.StartNew();
                var captured = capture();
                _lastCaptureMs = captureSw.Elapsed.TotalMilliseconds;
                var finalizeSw = Stopwatch.StartNew();
                _published = finalize(captured);
                _publishedAt = now;
                _sequence++;
                _lastFinalizeMs = finalizeSw.Elapsed.TotalMilliseconds;
                return _published;
            }

            private sealed class Waiter
            {
                public readonly ManualResetEventSlim Signal = new ManualResetEventSlim(false);
            }
        }

        private sealed class ValueRoute<TSnapshot> where TSnapshot : class
        {
            private readonly TimberbotJw _jw;
            private readonly Func<int, TSnapshot> _snapshotProvider;
            private readonly IValueSchema<TSnapshot> _schema;

            public ValueRoute(TimberbotJw jw, Func<int, TSnapshot> snapshotProvider, IValueSchema<TSnapshot> schema)
            {
                _jw = jw;
                _snapshotProvider = snapshotProvider;
                _schema = schema;
            }

            public object Collect(string format = "toon")
            {
                TSnapshot snapshot;
                try { snapshot = _snapshotProvider(2000); }
                catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
                if (snapshot == null) return _jw.Error("not_ready: snapshot not yet built, retry in 1s");
                var jw = _jw.Reset();
                _schema.Write(jw, format ?? "toon", snapshot);
                return jw.ToString();
            }
        }

        private interface IFlatArraySchema<TItem>
        {
            void WriteItem(TimberbotJw jw, string format, TItem item);
        }

        private sealed class FlatArrayRoute<TItem>
        {
            private readonly TimberbotJw _jw;
            private readonly Func<int, TItem[]> _itemsProvider;
            private readonly IFlatArraySchema<TItem> _schema;

            public FlatArrayRoute(TimberbotJw jw, Func<int, TItem[]> itemsProvider, IFlatArraySchema<TItem> schema)
            {
                _jw = jw;
                _itemsProvider = itemsProvider;
                _schema = schema;
            }

            public object Collect(string format = "toon", int limit = 100, int offset = 0)
            {
                TItem[] items;
                try { items = _itemsProvider(2000); }
                catch (TimeoutException) { return _jw.Error("refresh_timeout: server is building a fresh snapshot, retry in 1s"); }
                if (items == null) return _jw.Error("not_ready: snapshot not yet built, retry in 1s");

                bool paginated = limit > 0;
                int skipped = 0, emitted = 0;
                var jw = _jw.Reset().BeginArr();
                for (int i = 0; i < items.Length; i++)
                {
                    if (offset > 0 && skipped < offset) { skipped++; continue; }
                    if (paginated && emitted >= limit) break;
                    emitted++;
                    _schema.WriteItem(jw, format ?? "toon", items[i]);
                }
                return jw.End();
            }
        }

        private sealed class TimeSchema : IValueSchema<TimeSnapshot>
        {
            public void Write(TimberbotJw jw, string format, TimeSnapshot snapshot)
                => jw.OpenObj()
                    .Prop("dayNumber", snapshot.DayNumber)
                    .Prop("dayProgress", snapshot.DayProgress)
                    .Prop("partialDayNumber", snapshot.PartialDayNumber)
                    .CloseObj();
        }

        private sealed class WeatherSchema : IValueSchema<WeatherSnapshot>
        {
            public void Write(TimberbotJw jw, string format, WeatherSnapshot snapshot)
                => jw.OpenObj()
                    .Prop("cycle", snapshot.Cycle)
                    .Prop("cycleDay", snapshot.CycleDay)
                    .Prop("isHazardous", snapshot.IsHazardous)
                    .Prop("temperateWeatherDuration", snapshot.TemperateWeatherDuration)
                    .Prop("hazardousWeatherDuration", snapshot.HazardousWeatherDuration)
                    .Prop("cycleLengthInDays", snapshot.CycleLengthInDays)
                    .CloseObj();
        }

        private sealed class SpeedSchema : IValueSchema<SpeedSnapshot>
        {
            public void Write(TimberbotJw jw, string format, SpeedSnapshot snapshot)
                => jw.OpenObj().Prop("speed", snapshot.Speed).CloseObj();
        }

        private sealed class WorkHoursSchema : IValueSchema<WorkHoursSnapshot>
        {
            public void Write(TimberbotJw jw, string format, WorkHoursSnapshot snapshot)
                => jw.OpenObj().Prop("endHours", snapshot.EndHours).Prop("areWorkingHours", snapshot.AreWorkingHours).CloseObj();
        }

        private sealed class RawJsonSchema : IValueSchema<RawJsonSnapshot>
        {
            public void Write(TimberbotJw jw, string format, RawJsonSnapshot snapshot)
                => jw.Raw(snapshot.Json ?? "null");
        }

        private sealed class NotificationSchema : IFlatArraySchema<NotificationItem>
        {
            public void WriteItem(TimberbotJw jw, string format, NotificationItem item)
                => jw.OpenObj()
                    .Prop("subject", item.Subject)
                    .Prop("description", item.Description)
                    .Prop("cycle", item.Cycle)
                    .Prop("cycleDay", item.CycleDay)
                    .CloseObj();
        }

        private sealed class AlertSchema : IFlatArraySchema<AlertItem>
        {
            public void WriteItem(TimberbotJw jw, string format, AlertItem item)
            {
                jw.OpenObj().Prop("type", item.Type).Prop("id", item.Id).Prop("name", item.Name);
                if (!string.IsNullOrEmpty(item.Workers))
                    jw.Prop("workers", item.Workers);
                jw.CloseObj();
            }
        }

        private sealed class PowerSchema : IValueSchema<PowerNetworkItem[]>
        {
            public void Write(TimberbotJw jw, string format, PowerNetworkItem[] snapshot)
            {
                jw.OpenArr();
                if (snapshot != null)
                {
                    for (int i = 0; i < snapshot.Length; i++)
                    {
                        var net = snapshot[i];
                        jw.OpenObj().Prop("id", net.Id).Prop("supply", net.Supply).Prop("demand", net.Demand);
                        jw.Arr("buildings");
                        if (net.Buildings != null)
                        {
                            for (int bi = 0; bi < net.Buildings.Length; bi++)
                            {
                                var building = net.Buildings[bi];
                                jw.OpenObj()
                                    .Prop("name", building.Name)
                                    .Prop("id", building.Id)
                                    .Prop("isGenerator", building.IsGenerator)
                                    .Prop("nominalOutput", building.NominalOutput)
                                    .Prop("nominalInput", building.NominalInput)
                                    .CloseObj();
                            }
                        }
                        jw.CloseArr().CloseObj();
                    }
                }
                jw.CloseArr();
            }
        }
    }
}

