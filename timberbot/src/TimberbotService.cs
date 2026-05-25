// TimberbotService.cs. Core service: DI constructor, fields, lifecycle, settings.
//
// This is the main entry point for the Timberbot API mod. Timberborn's Bindito DI
// system injects game services into the constructor. The service runs as a game
// singleton: Load() starts the HTTP server, UpdateSingleton() drains queued POST
// requests on the Unity main thread and services fresh snapshot publishes.
//
// API logic lives in separate classes, each with their own DI:
//   TimberbotEntityRegistry. Entity lookup + tracked refs for writes and v2 snapshots
//   TimberbotWrite        . All POST write endpoints
//   TimberbotPlacement    . Building placement, path routing, terrain
//   TimberbotEvents       . [OnEvent] publishers → WS broadcaster
//   TimberbotDebug        . Reflection inspector and benchmark

using Timberborn.SingletonSystem;
using UnityEngine;
using Newtonsoft.Json.Linq;
using System.Reflection;
using Timberborn.NotificationSystem;
using Timberborn.BaseComponentSystem;

namespace Timberbot
{
    // HTTP API service. Injected via Bindito DI, runs as game singleton.
    // Returns plain objects serialized to JSON by TimberbotHttpServer.
    //
    // format param: "toon" (default) = flat for tabular display, "json" = full nested data
    // entity access: no typed queries in Timberborn, so we iterate _entityRegistry.Entities + GetComponent<T>()
    // names: CanonicalName() strips only "(Clone)"; public API names remain faction-qualified
    // entity lookup: Registry resolves numeric API IDs through Timberborn entity GUIDs
    public class TimberbotService : ILoadableSingleton, IUpdatableSingleton, IUnloadableSingleton
    {
        private readonly EventBus _eventBus;
        public readonly TimberbotEntityRegistry Registry;
        public readonly TimberbotReadV2 ReadV2;
        public readonly TimberbotEvents Events;
        public readonly TimberbotWrite Write;
        public readonly TimberbotPlacement Placement;
        public readonly TimberbotDebug DebugTool;
        // Widget/connector state container. Persisted fields live in
        // state.json (loaded in Load(), flushed by FlushAgentState).
        public readonly TimberbotAgentState AgentState = new TimberbotAgentState();
        private TimberbotHttpServer _server;
        private TimberbotWebSocketServer _wsServer;
        private string _agentStatePath;
        private float _agentStateDirtyTime = -1f;

        // Exposed so TimberbotPanel can build a localhost client URL to call the
        // new /api/agent/* + /api/ready endpoints owned by the state container
        // (Unit 1). The widget hits its own HTTP surface like any other client,
        // which keeps the gate semantics symmetrical with external connectors.
        public int HttpPort => _httpPort;
        public string ListenAddress => _listenAddress;
        // Exposed so TimberbotPanel can attach an `Authorization: Bearer` header
        // to its own /api/agent/state + /api/ready calls. When authToken is set
        // in settings.json, the panel is a regular HTTP client of its own server
        // and gets 401'd just like any external caller without this. Empty
        // string means auth is disabled (the loopback default).
        public string AuthToken => _authToken;

        // settings (loaded from settings.json in mod folder)
        private bool _debugEnabled = false;       // enable /api/debug endpoint (default: off)
        private int _httpPort = 8085;             // HTTP server port
        private int _wsPort = 8086;               // WebSocket server port (state + events)
        private bool _wsEnabled = true;           // toggle the WS server (events stop firing while off)
        public const int DefaultSearchRadius = 30; // Default radius for proximity searches
        private double _writeBudgetMs = 1.0;
        // security settings
        // IPv4 literal — avoids the `localhost` AAAA/A resolution split, which
        // can have HttpListener bind to ::1 on some platforms while clients
        // dial 127.0.0.1.
        private string _listenAddress = "127.0.0.1";
        private string _corsOrigin = "";
        private int _maxBodyBytes = 1048576;
        private bool _actionLoggingEnabled = true;
        // Bearer-token shared secret for /api/* requests. Empty = no
        // enforcement (only safe for loopback binds — see RequiresAuthToken).
        private string _authToken = "";
        private readonly NotificationBus _notificationBus;
        private string _settingsPath;            // full path to settings.json
        private JObject _cachedSettings;         // in-memory settings, flushed on debounce
        private float _settingsDirtyTime = -1f;  // realtimeSinceStartup when last mutated, -1 = clean

        public TimberbotService(
            EventBus eventBus,
            TimberbotEntityRegistry registry,
            TimberbotReadV2 readV2,
            TimberbotEvents events,
            TimberbotWrite write,
            TimberbotPlacement placement,
            TimberbotDebug debug,
            NotificationBus notificationBus)
        {
            _eventBus = eventBus;
            Registry = registry;
            ReadV2 = readV2;
            Events = events;
            Write = write;
            Placement = placement;
            DebugTool = debug;
            _notificationBus = notificationBus;
        }

        // Called once when a game is loaded. Starts the HTTP server and hooks into
        // the game's event system.
        //
        // Startup sequence:
        //   1. Load settings.json from mod folder (Documents/Timberborn/Mods/Timberbot/)
        //   2. Initialize logging (fresh log file per session)
        //   3. Wire up cross-references between subsystems (Registry<->Events, Debug<->Service)
        //   4. Register EventBus listeners (entity lifecycle, weather, buildings, etc)
        //   5. Build entity indexes from existing game state (all buildings/beavers/trees)
        //   6. Start HTTP server on configured port
        public void Load()
        {
            LoadSettings();
            LoadAgentState();
            var modDir = TimberbotPaths.ModDir;
            TimberbotLog.Init(modDir);

            // Refuse-to-start guard: a non-loopback bind without an authToken would
            // expose every /api/* mutation endpoint to anyone on the network. Bail
            // before opening the listener so an operator cannot accidentally ship
            // an unauthenticated, internet-reachable mod. The same guard covers
            // the WS port — both ports share `listenAddress`.
            if (TimberbotPure.RequiresAuthToken(_listenAddress, _authToken))
            {
                var msg = $"refusing to start: listenAddress='{_listenAddress}' is non-loopback but authToken is empty. " +
                          "Set authToken in settings.json (any non-empty string) or change listenAddress to localhost/127.0.0.1.";
                TimberbotLog.Info("startup.refused: " + msg);
                UnityEngine.Debug.LogError("[Timberbot] " + msg);
                return;
            }

            var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "unknown";
            TimberbotLog.Info($"v{version} port={_httpPort} wsPort={_wsPort} wsEnabled={_wsEnabled} debug={_debugEnabled} listen={_listenAddress} maxBody={_maxBodyBytes} authTokenSet={(!string.IsNullOrEmpty(_authToken))}");
            Registry.Events = Events;         // registry pushes WS events on entity lifecycle
            DebugTool.Service = this;         // debug needs Service reference for endpoint benchmarks
            _eventBus.Register(this);
            Events.Register();                // subscribe to ~70 game events
            ReadV2.Register();           // subscribe to entity lifecycle events for v2 snapshots
            Registry.Register();              // subscribe to entity lifecycle events
            Placement.DetectFaction();          // detect faction suffix. must run before BuildAllIndexes
            Registry.BuildAllIndexes();        // populate indexes from existing entities
            ReadV2.BuildAll();          // populate v2 building trackers from existing entities
            _server = new TimberbotHttpServer(_httpPort, this, _debugEnabled, _listenAddress, _corsOrigin, _maxBodyBytes, _authToken);
            TimberbotLog.Info($"HTTP server started on port {_httpPort}");
            if (_wsEnabled)
            {
                try
                {
                    _wsServer = new TimberbotWebSocketServer(_wsPort, AgentState, _listenAddress, _authToken);
                    Events.Broadcaster = _wsServer;
                    TimberbotLog.Info($"WS server started on port {_wsPort}");
                }
                catch (System.Exception ex)
                {
                    TimberbotLog.Error("ws.start", ex);
                }
            }
        }

        private void LoadSettings()
        {
            try
            {
                var path = TimberbotPaths.SettingsPath;
                _settingsPath = path;
                if (System.IO.File.Exists(path))
                {
                    var json = JObject.Parse(System.IO.File.ReadAllText(path));
                    _cachedSettings = json;
                    _debugEnabled = json.Value<bool>("debugEndpointEnabled");
                    _httpPort = json.Value<int>("httpPort");
                    if (_httpPort <= 0) _httpPort = 8085;
                    if (json["wsPort"] != null)
                    {
                        int p = json.Value<int>("wsPort");
                        _wsPort = p > 0 ? p : 8086;
                    }
                    if (json["wsEnabled"] != null)
                        _wsEnabled = json.Value<bool>("wsEnabled");
                    if (json["writeBudgetMs"] != null)
                    {
                        double budget = json.Value<double>("writeBudgetMs");
                        _writeBudgetMs = budget > 0 ? budget : 1.0;
                    }
                    // security settings
                    if (json["listenAddress"] != null)
                        _listenAddress = json.Value<string>("listenAddress") ?? "127.0.0.1";
                    if (json["corsOrigin"] != null)
                        _corsOrigin = json.Value<string>("corsOrigin") ?? "";
                    if (json["maxBodyBytes"] != null)
                    {
                        int mb = json.Value<int>("maxBodyBytes");
                        _maxBodyBytes = mb >= 0 ? mb : 1048576;
                    }
                    if (json["actionLoggingEnabled"] != null)
                        _actionLoggingEnabled = json.Value<bool>("actionLoggingEnabled");
                    if (json["authToken"] != null)
                        _authToken = TimberbotPure.NormalizeAuthToken(json.Value<string>("authToken"));

                    // PR 4: detect deprecated keys and log once. Values stay
                    // on disk this release so a future PR can strip them
                    // cleanly. The Python client no longer reads settings.json
                    // (impuls42/timberbot#43 PR 2), so the canonical list lives
                    // on the C# side at TimberbotPure.DEPRECATED_SETTINGS_KEYS.
                    var deprecated = TimberbotPure.DetectDeprecatedSettings(json);
                    if (deprecated.Count > 0)
                    {
                        TimberbotLog.Info(
                            "settings.deprecated: ignoring [" +
                            string.Join(", ", deprecated) +
                            "]; manage agent settings via ~/.config/timberbot/config.toml");
                    }
                }
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("settings.json load failed, using defaults", ex);
            }
        }

        public string GetUISetting(string key)
        {
            try
            {
                if (_cachedSettings != null)
                    return _cachedSettings.Value<string>(key);
            }
            catch { }
            return null;
        }

        public void SaveUISetting(string key, string value)
        {
            SaveSettingToken(key, value);
        }

        public void SaveBoolSetting(string key, bool value)
        {
            SaveSettingToken(key, value);
        }

        public void SaveIntSetting(string key, int value)
        {
            SaveSettingToken(key, value);
        }

        public void SaveDoubleSetting(string key, double value)
        {
            SaveSettingToken(key, value);
        }

        private void SaveSettingToken(string key, JToken value)
        {
            if (_settingsPath == null) return;
            if (_cachedSettings == null) _cachedSettings = new JObject();
            _cachedSettings[key] = value;
            _settingsDirtyTime = Time.realtimeSinceStartup;
        }

        private void FlushSettingsIfNeeded(float now)
        {
            if (_settingsDirtyTime < 0f) return;
            if (now - _settingsDirtyTime < 1f) return;
            FlushSettings();
        }

        private void FlushSettings()
        {
            if (_settingsDirtyTime < 0f || _cachedSettings == null || _settingsPath == null) return;
            _settingsDirtyTime = -1f;
            try
            {
                System.IO.File.WriteAllText(_settingsPath, _cachedSettings.ToString(Newtonsoft.Json.Formatting.Indented));
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("settings.json flush failed", ex);
            }
        }

        public void Unload()
        {
            FlushSettings();
            FlushAgentState();
            ReadV2.Unregister();
            Registry.Unregister();
            Events.Unregister();
            _eventBus.Unregister(this);
            _wsServer?.Stop();
            _wsServer = null;
            _server?.Stop();
            _server = null;
            TimberbotLog.Info("HTTP + WS servers stopped");
        }

        // Called by HTTP handlers when a persisted field on AgentState
        // changed. Writes are debounced (~1s after last change) just like
        // settings.json so a fast widget-driven sequence doesn't flush per
        // keystroke.
        public void MarkAgentStateDirty()
        {
            _agentStateDirtyTime = Time.realtimeSinceStartup;
        }

        private void LoadAgentState()
        {
            try
            {
                _agentStatePath = TimberbotPaths.StatePath;
                if (System.IO.File.Exists(_agentStatePath))
                {
                    var json = System.IO.File.ReadAllText(_agentStatePath);
                    if (!AgentState.LoadJson(json))
                        AgentState.ResetEphemerals();
                }
                else
                {
                    AgentState.ResetEphemerals();
                }
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("state.json load failed, using defaults", ex);
                AgentState.ResetEphemerals();
            }
        }

        private void FlushAgentStateIfNeeded(float now)
        {
            if (_agentStateDirtyTime < 0f) return;
            if (now - _agentStateDirtyTime < 1f) return;
            FlushAgentState();
        }

        private void FlushAgentState()
        {
            if (_agentStatePath == null) return;
            _agentStateDirtyTime = -1f;
            try
            {
                System.IO.File.WriteAllText(_agentStatePath, AgentState.ToJson());
            }
            catch (System.Exception ex)
            {
                TimberbotLog.Error("state.json flush failed", ex);
            }
        }

        // Called every frame by Unity. This is the mod's main loop.
        // It drains POST requests and processes pending fresh-read publishes.
        // Outbound game-event pushes go straight through the WS broadcaster
        // (TimberbotEvents.PushEvent → TimberbotWebSocketServer.PushEvent),
        // so there's no per-frame flush step any more.
        public void UpdateSingleton()
        {
            float now = Time.realtimeSinceStartup;
            _server?.DrainRequests();
            ReadV2.ProcessPendingRefresh(now);
            _server?.ProcessWriteJobs(now, _writeBudgetMs);
            FlushSettingsIfNeeded(now);
            FlushAgentStateIfNeeded(now);
        }
        public void PostNotification(string message, BaseComponent subject = null)
        {
            if (!_actionLoggingEnabled || _notificationBus == null) return;
            _notificationBus.Post(message, subject);
        }

        public bool ActionLoggingEnabled
        {
            get => _actionLoggingEnabled;
            set
            {
                _actionLoggingEnabled = value;
                if (Write != null) Write.InGameLoggingEnabled = value;
                SaveBoolSetting("actionLoggingEnabled", value);
            }
        }

        public event System.Action<string> OnAgentFeedback;
        public void PostAgentFeedback(string message) => OnAgentFeedback?.Invoke(message);
    }
}
