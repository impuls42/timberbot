// TimberbotWebhook.cs — game-event publisher.
//
// PRE-REWORK: this class managed registered outbound HTTP webhook URLs,
// batched event payloads on a 200ms cadence, and dispatched them via the
// ThreadPool with a circuit breaker.
//
// POST-REWORK (issue #28): the mod publishes events over the single
// WebSocket channel on `wsPort`. The class keeps its [OnEvent] handlers so
// the EventBus wiring is unchanged, but instead of accumulating payloads it
// calls `TimberbotWebSocketServer.PushEvent(name, day, ts, data)` directly.
// The WS broadcaster owns per-connection delivery + slow-consumer policy.
//
// FlushWebhooks/RegisterWebhook/UnregisterWebhook/ListWebhooks are gone —
// their HTTP routes were deleted in this PR.

using System;
using Timberborn.BlockSystem;
using Timberborn.EntitySystem;
using Timberborn.GameCycleSystem;
using Timberborn.SingletonSystem;
using Timberborn.TimeSystem;
using Timberborn.WeatherSystem;

namespace Timberbot
{
    public class TimberbotWebhook
    {
        private readonly IDayNightCycle _dayNightCycle;
        private readonly WeatherService _weatherService;
        private readonly GameCycleService _gameCycleService;
        private readonly SpeedManager _speedManager;
        private readonly EventBus _eventBus;

        // Set by TimberbotService.Load(); when null, all PushEvent calls
        // become no-ops (useful for early-boot ordering and tests).
        public TimberbotWebSocketServer Broadcaster;

        private readonly TimberbotJw _jw = new TimberbotJw(512);

        public TimberbotWebhook(
            IDayNightCycle dayNightCycle,
            WeatherService weatherService,
            GameCycleService gameCycleService,
            SpeedManager speedManager,
            EventBus eventBus)
        {
            _dayNightCycle = dayNightCycle;
            _weatherService = weatherService;
            _gameCycleService = gameCycleService;
            _speedManager = speedManager;
            _eventBus = eventBus;
        }

        public void Register() => _eventBus.Register(this);
        public void Unregister() => _eventBus.Unregister(this);

        // Publish an event with no data payload.
        public void PushEvent(string eventName)
        {
            var b = Broadcaster;
            if (b == null) return;
            b.PushEvent(eventName, _dayNightCycle.DayNumber, DateTimeOffset.UtcNow.ToUnixTimeSeconds(), null);
        }

        // Publish an event whose `data` payload is a pre-built JSON string.
        public void PushEvent(string eventName, string dataJson)
        {
            var b = Broadcaster;
            if (b == null) return;
            b.PushEvent(eventName, _dayNightCycle.DayNumber, DateTimeOffset.UtcNow.ToUnixTimeSeconds(), dataJson);
        }

        // helpers for building data JSON without anonymous objects
        private static string CanonicalName(string name) => TimberbotEntityRegistry.CanonicalName(name);

        public string DataInt(string key, int val) =>
            _jw.BeginObj().Key(key).Int(val).CloseObj().ToString();

        public string DataEntity(int id, string name) =>
            _jw.BeginObj().Prop("id", id).Prop("name", name).CloseObj().ToString();

        public string DataEntityBot(int id, string name, bool isBot) =>
            _jw.BeginObj().Prop("id", id).Prop("name", name).Prop("isBot", isBot).CloseObj().ToString();

        // ================================================================
        // WEBHOOK EVENT HANDLERS
        // ================================================================

        // weather
        [OnEvent] public void OnDroughtStart(Timberborn.HazardousWeatherSystem.HazardousWeatherStartedEvent e) => PushEvent("drought.start", DataInt("duration", _weatherService.HazardousWeatherDuration));
        [OnEvent] public void OnDroughtEnd(Timberborn.HazardousWeatherSystem.HazardousWeatherEndedEvent e) => PushEvent("drought.end");
        [OnEvent] public void OnDroughtApproaching(Timberborn.HazardousWeatherSystemUI.HazardousWeatherApproachingEvent e) => PushEvent("drought.approaching");
        [OnEvent] public void OnCycleStart(Timberborn.GameCycleSystem.CycleStartedEvent e) => PushEvent("cycle.start", DataInt("cycle", _gameCycleService.Cycle));
        [OnEvent] public void OnCycleEnd(Timberborn.GameCycleSystem.CycleEndedEvent e) => PushEvent("cycle.end", DataInt("cycle", _gameCycleService.Cycle));
        [OnEvent] public void OnCycleDay(Timberborn.GameCycleSystem.CycleDayStartedEvent e) => PushEvent("cycle.day", _jw.BeginObj().Prop("cycle", _gameCycleService.Cycle).Prop("cycleDay", _gameCycleService.CycleDay).CloseObj().ToString());

        // time
        [OnEvent] public void OnDayStart(Timberborn.TimeSystem.DaytimeStartEvent e) => PushEvent("day.start", DataInt("day", _dayNightCycle.DayNumber));
        [OnEvent] public void OnNightStart(Timberborn.TimeSystem.NighttimeStartEvent e) => PushEvent("night.start", DataInt("day", _dayNightCycle.DayNumber));

        // buildings
        [OnEvent] public void OnBuildingFinished(EnteredFinishedStateEvent e) { try { var go = e.BlockObject?.GetComponent<EntityComponent>()?.GameObject; PushEvent("building.finished", DataEntity(go?.GetInstanceID() ?? 0, go != null ? CanonicalName(go.name) : "")); } catch (Exception _ex) { TimberbotLog.Error("webhook.building_finished", _ex); } }
        [OnEvent] public void OnDistrictChanged(Timberborn.GameDistricts.DistrictCenterRegistryChangedEvent e) => PushEvent("district.changed");

        // population
        [OnEvent] public void OnPopulationChanged(Timberborn.Population.PopulationChangedEvent e) => PushEvent("population.changed");
        [OnEvent] public void OnCharacterCreated(Timberborn.Characters.CharacterCreatedEvent e) => PushEvent("character.created");
        [OnEvent] public void OnCharacterKilled(Timberborn.Characters.CharacterKilledEvent e) => PushEvent("character.killed");
        [OnEvent] public void OnBeaverBornEvt(Timberborn.Beavers.BeaverBornEvent e) => PushEvent("beaver.born.event");
        [OnEvent] public void OnBotManufactured(Timberborn.BotUpkeep.BotManufacturedEvent e) => PushEvent("bot.manufactured");
        [OnEvent] public void OnMigration(Timberborn.GameDistricts.MigrationEvent e) => PushEvent("migration");

        // needs/wellbeing
        [OnEvent] public void OnContaminationChanged(Timberborn.BeaverContaminationSystem.ContaminableContaminationChangedEvent e) => PushEvent("contamination.changed");
        [OnEvent] public void OnTeethChipped(Timberborn.Healthcare.TeethChippedEvent e) => PushEvent("teeth.chipped");
        [OnEvent] public void OnWellbeingHighscore(Timberborn.Wellbeing.NewWellbeingHighscoreEvent e) => PushEvent("wellbeing.highscore");
        [OnEvent] public void OnStatusAlert(Timberborn.StatusSystem.StatusAlertAddedEvent e) => PushEvent("status.alert");

        // trees/crops
        [OnEvent] public void OnTreeCut(Timberborn.Forestry.TreeCutEvent e) => PushEvent("tree.cut");
        [OnEvent] public void OnCuttableHarvested(Timberborn.Cutting.CuttableCutEvent e) => PushEvent("cuttable.cut", null);
        [OnEvent] public void OnTreeCuttingAreaChanged(Timberborn.Forestry.TreeCuttingAreaChangedEvent e) => PushEvent("cutting.area.changed", null);
        [OnEvent] public void OnTreeAddedToCuttingArea(Timberborn.Forestry.TreeAddedToCuttingAreaEvent e) => PushEvent("tree.marked", null);
        [OnEvent] public void OnCropPlanted(Timberborn.NaturalResources.NaturalResourcePlantedEvent e) => PushEvent("crop.planted", null);
        [OnEvent] public void OnPlantingMarked(Timberborn.Planting.PlantingAreaMarkedEvent e) => PushEvent("planting.marked", null);

        // wonders
        [OnEvent] public void OnWonderActivated(Timberborn.Wonders.WonderActivatedEvent e) => PushEvent("wonder.activated", null);
        [OnEvent] public void OnWonderCompleted(Timberborn.GameWonderCompletion.WonderCompletedEvent e) => PushEvent("wonder.completed", null);
        [OnEvent] public void OnWonderCountdown(Timberborn.GameWonderCompletion.WonderCompletionCountdownStartedEvent e) => PushEvent("wonder.countdown", null);

        // power
        [OnEvent] public void OnPowerNetworkCreated(Timberborn.MechanicalSystem.MechanicalGraphCreatedEvent e) => PushEvent("power.network.created", null);
        [OnEvent] public void OnPowerNetworkRemoved(Timberborn.MechanicalSystem.MechanicalGraphRemovedEvent e) => PushEvent("power.network.removed", null);

        // buildings (continued)
        [OnEvent] public void OnBuildingUnlocked(Timberborn.ScienceSystem.BuildingUnlockedEvent e) => PushEvent("building.unlocked", null);
        [OnEvent] public void OnBuildingDeconstructed(Timberborn.DeconstructionSystem.BuildingDeconstructedEvent e) => PushEvent("building.deconstructed", null);
        [OnEvent] public void OnDemolishableMarked(Timberborn.Demolishing.DemolishableMarkedEvent e) => PushEvent("demolish.marked", null);
        [OnEvent] public void OnDemolishableUnmarked(Timberborn.Demolishing.DemolishableUnmarkedEvent e) => PushEvent("demolish.unmarked", null);

        // game state
        [OnEvent] public void OnGameOver(Timberborn.GameOver.GameOverEvent e) => PushEvent("game.over", null);
        [OnEvent] public void OnSpeedChanged(CurrentSpeedChangedEvent e) => PushEvent("speed.changed", DataInt("speed", (int)_speedManager.CurrentSpeed));
        [OnEvent] public void OnWorkHoursChanged(Timberborn.WorkSystem.WorkingHoursChangedEvent e) => PushEvent("workhours.changed", null);
        [OnEvent] public void OnWorkHoursTransitioned(Timberborn.WorkSystem.WorkingHoursTransitionedEvent e) => PushEvent("workhours.transitioned", null);
        [OnEvent] public void OnAutosave(Timberborn.Autosaving.AutosaveEvent e) => PushEvent("autosave", null);

        // explosions
        [OnEvent] public void OnDynamiteDetonated(Timberborn.Explosions.DynamiteDetonatedEvent e) => PushEvent("explosion", null);
        [OnEvent] public void OnExplosionKill(Timberborn.Explosions.MortalDiedFromExplosionEvent e) => PushEvent("explosion.kill", null);

        // terrain
        [OnEvent] public void OnTerrainDestroyed(Timberborn.TerrainPhysics.TerrainDestroyedEvent e) => PushEvent("terrain.destroyed", null);
        [OnEvent] public void OnWindChanged(Timberborn.WindSystem.WindChangedEvent e) => PushEvent("wind.changed", null);

        // zipline
        [OnEvent] public void OnZiplineActivated(Timberborn.ZiplineSystem.ZiplineConnectionActivatedEvent e) => PushEvent("zipline.activated", null);

        // blocks
        [OnEvent] public void OnBlockSet(BlockObjectSetEvent e) => PushEvent("block.set", null);
        [OnEvent] public void OnBlockUnset(BlockObjectUnsetEvent e) => PushEvent("block.unset", null);
        [OnEvent] public void OnConstructionStarted(EnteredUnfinishedStateEvent e) => PushEvent("construction.started", null);
        [OnEvent] public void OnBuildingUnfinished(ExitedFinishedStateEvent e) => PushEvent("building.unfinished", null);

        // entities
        [OnEvent] public void OnEntityCreated(EntityCreatedEvent e) => PushEvent("entity.created", null);

        // factions
        [OnEvent] public void OnFactionUnlocked(Timberborn.FactionSystem.FactionUnlockedEvent e) => PushEvent("faction.unlocked", null);

        // districts
        [OnEvent] public void OnDistrictConnectionsChanged(Timberborn.GameDistricts.DistrictConnectionsChangedEvent e) => PushEvent("district.connections.changed", null);
        [OnEvent] public void OnMigrationDistrictChanged(Timberborn.GameDistrictsMigration.MigrationDistrictChangedEvent e) => PushEvent("migration.district.changed", null);

        // weather (selection)
        [OnEvent] public void OnWeatherSelected(Timberborn.HazardousWeatherSystem.HazardousWeatherSelectedEvent e) => PushEvent("weather.selected", null);

        // power (detailed)
        [OnEvent] public void OnPowerGeneratorAdded(Timberborn.MechanicalSystem.MechanicalGraphGeneratorAddedEvent e) => PushEvent("power.generator.added", null);
        [OnEvent] public void OnPowerGeneratorUpdated(Timberborn.MechanicalSystem.MechanicalGraphGeneratorUpdatedEvent e) => PushEvent("power.generator.updated", null);

        // planting
        [OnEvent] public void OnPlantingCoordsSet(Timberborn.Planting.PlantingCoordinatesSetEvent e) => PushEvent("planting.coords.set", null);
        [OnEvent] public void OnPlantingCoordsUnset(Timberborn.Planting.PlantingCoordinatesUnsetEvent e) => PushEvent("planting.coords.unset", null);

        // game startup
        [OnEvent] public void OnNewGame(Timberborn.Common.NewGameInitializedEvent e) => PushEvent("game.new", null);
        [OnEvent] public void OnStartingBuilding(Timberborn.GameStartup.StartingBuildingPlacedEvent e) => PushEvent("game.starting.building", null);
        [OnEvent] public void OnSpeedLockChanged(SpeedLockChangedEvent e) => PushEvent("speed.lock.changed", null);

        // naming + alerts
        [OnEvent] public void OnEntityRenamed(Timberborn.EntityNaming.EntityNameChangedEvent e) => PushEvent("entity.renamed", null);
        [OnEvent] public void OnDynamicAlert(Timberborn.StatusSystem.DynamicStatusAlertAddedEvent e) => PushEvent("status.dynamic.alert", null);

        // construction mode
        [OnEvent] public void OnConstructionMode(Timberborn.ConstructionMode.ConstructionModeChangedEvent e) => PushEvent("construction.mode.changed", null);
    }
}
