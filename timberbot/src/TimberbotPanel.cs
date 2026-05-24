// TimberbotPanel.cs. In-game UI: connection-state pill + Launch/Stop gate + mode-aware agent tab.
//
// The widget no longer spawns a subprocess. Instead it is a thin client over
// the mod's own HTTP surface (Unit 1 in the rework — issue #13):
//
//   GET  /api/agent/state    -> {mode, goal, ready, pendingRequest, agentStatus, lastError}
//   POST /api/agent/config   {mode?, goal?}      -> persist widget edits
//   POST /api/agent/request  {prompt}            -> queue a one-shot request in request-mode
//   POST /api/ready          {ready: bool}       -> Launch / Stop ready gate
//
// The widget polls /api/agent/state every ~500ms from a background ThreadPool
// task (HttpClient.SendAsync) and stashes the latest parsed snapshot in
// _latestState. UpdateSingleton runs on the Unity main thread and reads that
// snapshot to drive UI labels and the connection-state pill colour.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using Timberborn.CoreUI;
using Timberborn.SingletonSystem;
using Timberborn.UILayoutSystem;
using UnityEngine;
using UnityEngine.UIElements;

namespace Timberbot
{
    public class TimberbotPanel : ILoadableSingleton, IUpdatableSingleton
    {
        private readonly UILayout _layout;
        private readonly TimberbotService _service;
        private readonly VisualElementInitializer _veInit;

        private VisualElement _widget;
        private Label _statusPill;
        private Label _statusBarLabel;
        private Label _widgetBanner;
        private NineSliceButton _widgetLaunchBtn;
        private NineSliceButton _widgetStopBtn;
        private NineSliceButton _widgetEditBtn;
        private NineSliceButton _widgetMinimizeBtn;
        private VisualElement _widgetButtonRow;
        private bool _widgetMinimized;

        private VisualElement _modalOverlay;
        private VisualElement _modalPanel;

        private VisualElement _settingsContainer;
        private VisualElement _agentSettingsContainer;
        private VisualElement _runtimeSettingsContainer;
        private VisualElement _securitySettingsContainer;
        private NineSliceButton _agentTabBtn;
        private NineSliceButton _startupTabBtn;
        private NineSliceButton _securityTabBtn;

        // Agent tab — Unit 6 layout. The Backend / Model / Effort knobs moved to
        // ~/.config/timberbot/config.toml in PR 4; the in-game tab now exposes
        // the runtime state the player can change: mode + the mode-bound text
        // buffer (Goal in autonomous mode, request prompt in request mode).
        private TextField _modeField;
        private NineSliceButton _modePresetBtn;
        private TextField _textareaField;
        private NineSliceButton _modalLaunchBtn;
        private string _lastTextareaSavedValue;
        private float _goalDirtyTime = -1f;
        private string _currentMode = ModeAutonomous;
        // Request-mode draft survives a mode flip-flop. Cleared only on
        // Launch (the prompt has been sent) so the player never loses a
        // typed prompt by accidentally toggling the dropdown.
        private string _requestDraft = "";

        private TextField _debugEndpointField;
        private NineSliceButton _debugEndpointPresetBtn;
        private TextField _httpPortField;
        private TextField _wsPortField;
        private TextField _wsEnabledField;
        private NineSliceButton _wsEnabledPresetBtn;
        private TextField _writeBudgetMsField;

        // security tab fields
        private TextField _listenAddressField;
        private NineSliceButton _listenAddressPresetBtn;
        private TextField _corsOriginField;
        private NineSliceButton _corsOriginPresetBtn;
        private TextField _maxBodyBytesField;

        // Console Widget
        private VisualElement _consoleRoot;
        private ScrollView _consoleScroll;
        private bool _consoleIsDragging;
        private bool _consoleCollapsed;
        private TextField _actionLoggingEnabledField;
        private NineSliceButton _actionLoggingEnabledPresetBtn;

        private VisualElement _presetPopup;
        private ScrollView _presetScroll;
        private VisualElement _presetPopupAnchor;
        private VisualElement _tooltipPopup;
        private Label _tooltipLabel;
        private VisualElement _tooltipAnchor;
        private string _pendingTooltipText;
        private VisualElement _pendingTooltipAnchor;
        private Vector2 _tooltipPointerPosition;
        private int _tooltipRequestId;

        private bool _isWidgetDragging;
        private int _dragPointerId;
        private Vector2 _dragStartPointer;
        private Vector2 _dragStartWidget;
        private float _preMinimizeLeft;
        private float _preMinimizeTop;
        private bool _hadPreMinimizePosition;
        private bool _widgetPositionInitialized;

        private float _lastUpdate;
        private string _activeSettingsTab = "agent";

        // State-polling machinery. UpdateSingleton kicks off a /api/agent/state
        // fetch every ~500ms on a background ThreadPool task and stashes the
        // result for the next UI tick to render. _pollInFlight prevents
        // overlapping polls if the server stalls.
        private static readonly TimeSpan PollInterval = TimeSpan.FromMilliseconds(500);
        private static readonly HttpClient _http = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
        private string _baseUrl;
        private DateTime _lastPollAt = DateTime.MinValue;
        private volatile int _pollInFlight;
        private volatile JObject _latestState;
        private volatile bool _lastPollOk;

        // Agent mode values land in /api/agent/config {mode: ...} verbatim
        // and must match the openapi.yaml enum on the server side.
        private const string ModeAutonomous = "autonomous";
        private const string ModeRequest = "request";

        private static readonly string[][] ModeChoices = new[]
        {
            new[] { "Autonomous", ModeAutonomous },
            new[] { "Request",    ModeRequest },
        };

        private static readonly string[][] BoolChoices = new[]
        {
            new[] { "true", "true" },
            new[] { "false", "false" },
        };

        private static readonly string[][] ListenAddressChoices = new[]
        {
            new[] { "localhost", "localhost" },
            new[] { "+ (all interfaces)", "+" },
        };

        private static readonly string[][] CorsOriginChoices = new[]
        {
            new[] { "(auto: localhost)", "" },
            new[] { "* (any origin)", "*" },
        };

        private const string DefaultGoal = "reach 50 beavers with 77 well-being";

        // Connection-state pill colours. Hex chosen to match the four other
        // game-text-* swatches so the pill reads as a status bar element, not
        // a foreign UI bolt-on.
        private static readonly Color PillColorDisconnected = new Color(0.85f, 0.30f, 0.30f); // red
        private static readonly Color PillColorNotReady     = new Color(0.95f, 0.80f, 0.25f); // yellow
        private static readonly Color PillColorIdle         = new Color(0.40f, 0.80f, 0.45f); // green
        private static readonly Color PillColorRunning      = new Color(0.40f, 0.65f, 0.95f); // blue
        private static readonly Color PillColorError        = new Color(0.95f, 0.55f, 0.20f); // orange

        private static readonly Dictionary<string, string> SettingTooltips = new Dictionary<string, string>
        {
            ["Mode:"] = "Autonomous: the agent works toward Goal continuously. Request: the agent stays idle until you click Launch to dispatch a one-shot prompt.",
            ["Goal / Prompt:"] = "Autonomous mode: long-running objective. Request mode: the next one-shot prompt — cleared after Launch sends it.",
            ["actionLoggingEnabled:"] = "Logs agent write/placement actions to the in-game console panel. Takes effect immediately.",
            ["debugEndpointEnabled:"] = "Enables debug and benchmark endpoints such as /api/debug and /api/benchmark. Reload save to apply.",
            ["httpPort:"] = "HTTP server port Timberbot listens on. The Python client reads this by default from settings.json. Reload save to apply.",
            ["wsPort:"] = "WebSocket server port for state + game-event push (replaces outbound HTTP webhooks). Reload save to apply.",
            ["wsEnabled:"] = "Toggle the WebSocket server. While off, connectors must drive everything off HTTP polling of /api/agent/state. Reload save to apply.",
            ["writeBudgetMs:"] = "Per-frame main-thread time budget for queued write jobs. Higher values process writes faster but use more frame time. Reload save to apply.",
            ["listenAddress:"] = "Network address the HTTP + WS servers bind to. 'localhost' (default) = local only. '+' = all interfaces (LAN access). Reload save to apply.",
            ["authToken:"] = "Bearer token required on every /api/* request except /api/ping, and on the WS upgrade. Empty = no auth (default; only safe on loopback). Required when listenAddress is non-loopback — the mod refuses to start otherwise. Reload save to apply.",
            ["corsOrigin:"] = "Allowed CORS origin for browser requests. Empty = auto (localhost only). '*' = any origin (less secure). Reload save to apply.",
            ["maxBodyBytes:"] = "Maximum POST request body size in bytes. 0 = unlimited. Default 1048576 (1MB). Reload save to apply.",
        };

        public TimberbotPanel(UILayout layout, TimberbotService service, VisualElementInitializer veInit)
        {
            _layout = layout;
            _service = service;
            _veInit = veInit;
        }

        public void Load()
        {
            // Always target the loopback interface — the widget runs in-process
            // with the HttpListener, and the default settings.json bind is
            // 127.0.0.1, so even when listenAddress is set to "+"/"0.0.0.0" the
            // loopback path still works and avoids LAN round-trips.
            _baseUrl = $"http://127.0.0.1:{_service.HttpPort}";

            BuildWidget();
            BuildModal();
            BuildConsole();

            _veInit.InitializeVisualElement(_widget);
            _veInit.InitializeVisualElement(_modalOverlay);
            _veInit.InitializeVisualElement(_consoleRoot);

            _layout.AddAbsoluteItem(_widget);
            _layout.AddAbsoluteItem(_modalOverlay);
            _layout.AddAbsoluteItem(_consoleRoot);

            _widget.ToggleDisplayStyle(true);
            _modalOverlay.ToggleDisplayStyle(false);
            UpdateConsoleVisibility();
            
            if (_service.Write != null)
                _service.Write.OnActionLog += HandleActionLog;
            if (_service.Placement != null)
                _service.Placement.OnActionLog += HandleActionLog;

            TimberbotLog.Info("panel: attached to game UI");
        }

        public void UpdateSingleton()
        {
            if (_widget == null)
                return;

            MaybeKickStatePoll();
            FlushPendingGoalSave();
            RefreshConnectionUi();
        }

        // Fires off a /api/agent/state fetch every PollInterval. The fetch runs
        // on a ThreadPool task so the Unity main thread keeps ticking even if
        // the listener stalls (the listener itself answers GETs off the main
        // thread, but the timeout-budget guarantee is still nice to have).
        private void MaybeKickStatePoll()
        {
            var now = DateTime.UtcNow;
            if (now - _lastPollAt < PollInterval) return;
            if (Interlocked.CompareExchange(ref _pollInFlight, 1, 0) != 0) return;
            _lastPollAt = now;

            _ = Task.Run(async () =>
            {
                try
                {
                    using var req = NewRequest(HttpMethod.Get, "/api/agent/state");
                    using var resp = await _http.SendAsync(req).ConfigureAwait(false);
                    var body = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!resp.IsSuccessStatusCode)
                    {
                        _lastPollOk = false;
                        return;
                    }
                    _latestState = JObject.Parse(body);
                    _lastPollOk = true;
                }
                catch (Exception)
                {
                    // Connector likely not registered yet, or the listener
                    // is mid-restart. The pill drops to Disconnected; we
                    // don't spam the log because poll failure is the steady
                    // state when no game session is bound.
                    _lastPollOk = false;
                }
                finally
                {
                    Interlocked.Exchange(ref _pollInFlight, 0);
                }
            });
        }

        // Autonomous-mode goal: debounced save. The textarea triggers a value
        // event every keystroke; we mark the time and only push to the server
        // after 1s of quiet — matches the runtime-settings debounce cadence.
        private void FlushPendingGoalSave()
        {
            if (_goalDirtyTime < 0f) return;
            if (Time.realtimeSinceStartup - _goalDirtyTime < 1f) return;
            _goalDirtyTime = -1f;
            if (_currentMode != ModeAutonomous) return;

            var goal = _textareaField?.value ?? "";
            if (goal == _lastTextareaSavedValue) return;
            _lastTextareaSavedValue = goal;
            _service.SaveUISetting("agentGoal", goal);  // local mirror for the next session boot
            PostJsonAsync("/api/agent/config", new JObject { ["goal"] = goal });
        }

        private void RefreshConnectionUi()
        {
            float now = Time.realtimeSinceStartup;
            if (now - _lastUpdate < 0.25f)
                return;
            _lastUpdate = now;

            var state = _latestState;
            var pollOk = _lastPollOk;
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(pollOk, state);

            if (_statusPill != null)
            {
                _statusPill.text = PillText(pill);
                _statusPill.style.backgroundColor = PillColor(pill);
            }
            if (_statusBarLabel != null)
                _statusBarLabel.text = "Timberbot API";
            if (_widgetBanner != null)
            {
                bool showBanner = pollOk && !gateOn;
                _widgetBanner.ToggleDisplayStyle(showBanner);
            }
            if (_widgetLaunchBtn != null)
                _widgetLaunchBtn.SetEnabled(pollOk && !gateOn);
            if (_widgetStopBtn != null)
                _widgetStopBtn.SetEnabled(pollOk && gateOn);

            // Mirror the server-side mode + goal into the modal fields when the
            // tab is open and the user isn't actively typing. The dirty-time
            // gate prevents the poll-loop from clobbering uncommitted edits.
            if (state != null && _modeField != null && _goalDirtyTime < 0f)
            {
                var serverMode = state.Value<string>("mode") ?? ModeAutonomous;
                if (serverMode != _modeField.value)
                {
                    _modeField.SetValueWithoutNotify(serverMode);
                    SyncTextareaForMode(serverMode, state);
                }
                if (serverMode == ModeAutonomous)
                {
                    var serverGoal = state.Value<string>("goal") ?? "";
                    if (_textareaField != null && _textareaField.value != serverGoal)
                    {
                        _textareaField.SetValueWithoutNotify(serverGoal);
                        _lastTextareaSavedValue = serverGoal;
                    }
                }
            }
        }

        private static string PillText(TimberbotPure.ConnectionPillState pill)
        {
            switch (pill)
            {
                case TimberbotPure.ConnectionPillState.Disconnected: return "Disconnected";
                case TimberbotPure.ConnectionPillState.NotReady:     return "Not Ready";
                case TimberbotPure.ConnectionPillState.Idle:         return "Idle";
                case TimberbotPure.ConnectionPillState.Running:      return "Running";
                case TimberbotPure.ConnectionPillState.Error:        return "Error";
                default:                                             return "Disconnected";
            }
        }

        private static Color PillColor(TimberbotPure.ConnectionPillState pill)
        {
            switch (pill)
            {
                case TimberbotPure.ConnectionPillState.Disconnected: return PillColorDisconnected;
                case TimberbotPure.ConnectionPillState.NotReady:     return PillColorNotReady;
                case TimberbotPure.ConnectionPillState.Idle:         return PillColorIdle;
                case TimberbotPure.ConnectionPillState.Running:      return PillColorRunning;
                case TimberbotPure.ConnectionPillState.Error:        return PillColorError;
                default:                                             return PillColorDisconnected;
            }
        }

        private void BuildWidget()
        {
            _widget = new NineSliceVisualElement();
            _widget.AddToClassList("top-right-item");
            _widget.AddToClassList("square-large--green");
            _widget.style.position = Position.Absolute;
            _widget.style.flexDirection = FlexDirection.Column;
            _widget.style.alignItems = Align.Stretch;
            _widget.style.paddingLeft = 6;
            _widget.style.paddingRight = 6;
            _widget.style.paddingTop = 4;
            _widget.style.paddingBottom = 4;
            _widgetMinimized = _service.GetUISetting("widgetMinimized") == "true";
            if (_widgetMinimized)
            {
                _widget.style.right = 10;
                _widget.style.bottom = 10;
            }
            else
            {
                ApplySavedWidgetPosition();
            }

            var headerRow = new VisualElement();
            headerRow.style.flexDirection = FlexDirection.Row;
            headerRow.style.alignItems = Align.Center;

            _statusPill = new Label("Disconnected");
            _statusPill.AddToClassList("game-text-normal");
            _statusPill.AddToClassList("text--bold");
            _statusPill.style.color = Color.white;
            _statusPill.style.backgroundColor = PillColorDisconnected;
            _statusPill.style.paddingLeft = 8;
            _statusPill.style.paddingRight = 8;
            _statusPill.style.paddingTop = 2;
            _statusPill.style.paddingBottom = 2;
            _statusPill.style.marginRight = 6;
            _statusPill.style.borderTopLeftRadius = 6;
            _statusPill.style.borderTopRightRadius = 6;
            _statusPill.style.borderBottomLeftRadius = 6;
            _statusPill.style.borderBottomRightRadius = 6;
            headerRow.Add(_statusPill);

            _statusBarLabel = new NineSliceLabel { text = "Timberbot API" };
            _statusBarLabel.AddToClassList("text--yellow");
            _statusBarLabel.AddToClassList("game-text-normal");
            _statusBarLabel.style.flexGrow = 1;
            _statusBarLabel.RegisterCallback<PointerDownEvent>(OnWidgetPointerDown);
            _statusBarLabel.RegisterCallback<PointerMoveEvent>(OnWidgetPointerMove);
            _statusBarLabel.RegisterCallback<PointerUpEvent>(OnWidgetPointerUp);
            headerRow.Add(_statusBarLabel);
            _widgetMinimizeBtn = MakeGameButton(_widgetMinimized ? "+" : "-", OnMinimizeClicked);
            _widgetMinimizeBtn.style.width = 24;
            _widgetMinimizeBtn.style.height = 20;
            _widgetMinimizeBtn.style.marginLeft = 4;
            _widgetMinimizeBtn.style.paddingLeft = 0;
            _widgetMinimizeBtn.style.paddingRight = 0;
            headerRow.Add(_widgetMinimizeBtn);

            _widget.Add(headerRow);

            // Banner shown when the connector is talking to us but the gate is
            // off — tells the player the connector is alive and waiting for
            // them to flip Launch.
            _widgetBanner = new NineSliceLabel { text = "Connected to game session — waiting for player to Launch." };
            _widgetBanner.AddToClassList("text--green");
            _widgetBanner.AddToClassList("game-text-normal");
            _widgetBanner.style.whiteSpace = WhiteSpace.Normal;
            _widgetBanner.style.maxWidth = 260;
            _widgetBanner.style.marginTop = 4;
            _widgetBanner.style.display = DisplayStyle.None;
            _widget.Add(_widgetBanner);

            _widgetButtonRow = new VisualElement();
            _widgetButtonRow.style.flexDirection = FlexDirection.Row;
            _widgetButtonRow.style.justifyContent = Justify.Center;
            _widgetButtonRow.style.alignItems = Align.Center;
            _widgetButtonRow.style.marginTop = 4;
            _widgetButtonRow.style.display = _widgetMinimized ? DisplayStyle.None : DisplayStyle.Flex;

            _widgetLaunchBtn = MakeGameButton("Launch", OnLaunchClicked);
            _widgetLaunchBtn.style.width = 72;
            _widgetLaunchBtn.style.marginRight = 4;
            _widgetButtonRow.Add(_widgetLaunchBtn);

            _widgetStopBtn = MakeGameButton("Stop", OnStopClicked);
            _widgetStopBtn.style.width = 58;
            _widgetStopBtn.style.marginRight = 4;
            _widgetStopBtn.SetEnabled(false);
            _widgetButtonRow.Add(_widgetStopBtn);

            _widgetEditBtn = MakeGameButton("Settings", ShowModal);
            _widgetEditBtn.style.width = 78;
            _widgetButtonRow.Add(_widgetEditBtn);

            _widget.Add(_widgetButtonRow);
        }

        private void BuildModal()
        {
            _modalOverlay = new VisualElement();
            _modalOverlay.style.position = Position.Absolute;
            _modalOverlay.style.left = 0;
            _modalOverlay.style.top = 0;
            _modalOverlay.style.right = 0;
            _modalOverlay.style.bottom = 0;
            _modalOverlay.style.justifyContent = Justify.Center;
            _modalOverlay.style.alignItems = Align.Center;
            _modalOverlay.style.backgroundColor = new Color(0f, 0f, 0f, 0.25f);
            _modalOverlay.RegisterCallback<PointerDownEvent>(OnOverlayPointerDown);

            _modalPanel = new NineSliceVisualElement();
            _modalPanel.AddToClassList("bg-sub-box--green");
            _modalPanel.style.width = 420;
            _modalPanel.style.maxHeight = 620;
            _modalPanel.style.paddingTop = 8;
            _modalPanel.style.paddingBottom = 8;
            _modalPanel.style.paddingLeft = 10;
            _modalPanel.style.paddingRight = 10;
            _modalPanel.style.flexDirection = FlexDirection.Column;
            _modalPanel.style.overflow = Overflow.Visible;
            _modalOverlay.Add(_modalPanel);

            var header = new VisualElement();
            header.style.flexDirection = FlexDirection.Row;
            header.style.justifyContent = Justify.SpaceBetween;
            header.style.alignItems = Align.Center;
            header.style.marginBottom = 6;

            var title = new NineSliceLabel { text = "Timberbot API - Settings" };
            title.AddToClassList("text--yellow");
            title.AddToClassList("game-text-normal");
            title.AddToClassList("text--bold");
            header.Add(title);

            var closeBtn = new NineSliceButton();
            closeBtn.AddToClassList("button-square");
            closeBtn.AddToClassList("button-square--small");
            closeBtn.AddToClassList("button-minus");
            closeBtn.clicked += HideModal;
            header.Add(closeBtn);
            _modalPanel.Add(header);

            var content = new ScrollView(ScrollViewMode.Vertical);
            content.style.flexGrow = 1;
            content.style.maxHeight = 620;
            content.style.paddingRight = 4;
            _modalPanel.Add(content);


            _settingsContainer = new VisualElement();
            _settingsContainer.style.flexDirection = FlexDirection.Column;
            _settingsContainer.style.marginBottom = 6;
            content.Add(_settingsContainer);

            var tabRow = new VisualElement();
            tabRow.style.flexDirection = FlexDirection.Row;
            tabRow.style.marginBottom = 6;
            _settingsContainer.Add(tabRow);

            _agentTabBtn = MakeGameButton("Agent", ShowAgentTab);
            _agentTabBtn.style.width = 80;
            _agentTabBtn.style.marginRight = 4;
            tabRow.Add(_agentTabBtn);

            _startupTabBtn = MakeGameButton("Startup", ShowRuntimeTab);
            _startupTabBtn.style.width = 92;
            _startupTabBtn.style.marginRight = 4;
            tabRow.Add(_startupTabBtn);

            _securityTabBtn = MakeGameButton("Security", ShowSecurityTab);
            _securityTabBtn.style.width = 92;
            tabRow.Add(_securityTabBtn);

            _agentSettingsContainer = new VisualElement();
            _agentSettingsContainer.style.flexDirection = FlexDirection.Column;
            _settingsContainer.Add(_agentSettingsContainer);

            _runtimeSettingsContainer = new VisualElement();
            _runtimeSettingsContainer.style.flexDirection = FlexDirection.Column;
            _settingsContainer.Add(_runtimeSettingsContainer);

            _securitySettingsContainer = new VisualElement();
            _securitySettingsContainer.style.flexDirection = FlexDirection.Column;
            _settingsContainer.Add(_securitySettingsContainer);

            var savedGoal = _service.GetUISetting("agentGoal") ?? DefaultGoal;
            var savedActionLoggingEnabled = NormalizeBoolString(_service.GetUISetting("actionLoggingEnabled"), true);
            var savedDebugEndpointEnabled = NormalizeBoolString(_service.GetUISetting("debugEndpointEnabled"), false);
            var savedHttpPort = NormalizeValue(_service.GetUISetting("httpPort"), "8085");
            var savedWsPort = NormalizeValue(_service.GetUISetting("wsPort"), "8086");
            var savedWsEnabled = NormalizeBoolString(_service.GetUISetting("wsEnabled"), true);
            var savedWriteBudgetMs = NormalizeValue(_service.GetUISetting("writeBudgetMs"), "1.0");

            _agentSettingsContainer.Add(MakeHintLabel(
                "Backend / model / effort moved to ~/.config/timberbot/config.toml. " +
                "Use the Launch button to flip the ready gate; the connector handles spawning the agent."));

            _modeField = MakeTextField(ModeAutonomous);
            _modeField.RegisterValueChangedCallback(evt =>
            {
                var mode = NormalizeMode(evt.newValue);
                _modeField.SetValueWithoutNotify(mode);
                if (mode == _currentMode) return;
                _currentMode = mode;
                SyncTextareaForMode(mode, _latestState);
                PostJsonAsync("/api/agent/config", new JObject { ["mode"] = mode });
            });
            _modePresetBtn = MakePresetButton("v", () => TogglePresetMenu(_modePresetBtn, _modeField, ModeChoices));
            _agentSettingsContainer.Add(MakePresetFieldRow("Mode:", _modeField, _modePresetBtn));

            _textareaField = MakeTextField(savedGoal);
            _textareaField.multiline = true;
            _textareaField.style.height = 100;
            _textareaField.style.flexShrink = 1;
            _textareaField.style.maxWidth = 240;
            _textareaField.style.whiteSpace = WhiteSpace.Normal;
            var textareaInner = _textareaField.Q("unity-text-input");
            if (textareaInner != null)
                textareaInner.style.whiteSpace = WhiteSpace.Normal;
            _textareaField.RegisterValueChangedCallback(evt =>
            {
                if (_currentMode == ModeRequest)
                    _requestDraft = evt.newValue ?? "";
                else
                    _goalDirtyTime = Time.realtimeSinceStartup;
            });
            _lastTextareaSavedValue = savedGoal;
            _agentSettingsContainer.Add(MakeFieldRow("Goal / Prompt:", _textareaField));

            var agentActionRow = new VisualElement();
            agentActionRow.style.flexDirection = FlexDirection.Row;
            agentActionRow.style.justifyContent = Justify.FlexEnd;
            agentActionRow.style.marginTop = 6;
            _modalLaunchBtn = MakeGameButton("Launch", OnModalLaunchClicked);
            _modalLaunchBtn.style.width = 78;
            agentActionRow.Add(_modalLaunchBtn);
            _agentSettingsContainer.Add(agentActionRow);

            _runtimeSettingsContainer.Add(MakeHintLabel("Timberborn must be restarted or save loaded after changing these settings."));

            _actionLoggingEnabledField = MakeTextField(savedActionLoggingEnabled);
            _actionLoggingEnabledField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeBoolString(evt.newValue, true);
                _actionLoggingEnabledField.SetValueWithoutNotify(value);
                _service.ActionLoggingEnabled = (value == "true");
                UpdateConsoleVisibility();
            });
            _actionLoggingEnabledPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_actionLoggingEnabledPresetBtn, _actionLoggingEnabledField, BoolChoices));
            _runtimeSettingsContainer.Add(MakePresetFieldRow("actionLoggingEnabled:", _actionLoggingEnabledField, _actionLoggingEnabledPresetBtn));

            _debugEndpointField = MakeTextField(savedDebugEndpointEnabled);
            _debugEndpointField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeBoolString(evt.newValue, true);
                _debugEndpointField.SetValueWithoutNotify(value);
                _service.SaveBoolSetting("debugEndpointEnabled", value == "true");
            });
            _debugEndpointPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_debugEndpointPresetBtn, _debugEndpointField, BoolChoices));
            _runtimeSettingsContainer.Add(MakePresetFieldRow("debugEndpointEnabled:", _debugEndpointField, _debugEndpointPresetBtn));

            _httpPortField = MakeTextField(savedHttpPort);
            _httpPortField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeIntString(evt.newValue, 8085, 1);
                _httpPortField.SetValueWithoutNotify(value);
                _service.SaveIntSetting("httpPort", int.Parse(value));
            });
            _runtimeSettingsContainer.Add(MakeFieldRow("httpPort:", _httpPortField));

            _wsPortField = MakeTextField(savedWsPort);
            _wsPortField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeIntString(evt.newValue, 8086, 1);
                _wsPortField.SetValueWithoutNotify(value);
                _service.SaveIntSetting("wsPort", int.Parse(value));
            });
            _runtimeSettingsContainer.Add(MakeFieldRow("wsPort:", _wsPortField));

            _wsEnabledField = MakeTextField(savedWsEnabled);
            _wsEnabledField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeBoolString(evt.newValue, true);
                _wsEnabledField.SetValueWithoutNotify(value);
                _service.SaveBoolSetting("wsEnabled", value == "true");
            });
            _wsEnabledPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_wsEnabledPresetBtn, _wsEnabledField, BoolChoices));
            _runtimeSettingsContainer.Add(MakePresetFieldRow("wsEnabled:", _wsEnabledField, _wsEnabledPresetBtn));

            _writeBudgetMsField = MakeTextField(savedWriteBudgetMs);
            _writeBudgetMsField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeDoubleString(evt.newValue, 1.0, 0.001);
                _writeBudgetMsField.SetValueWithoutNotify(value);
                _service.SaveDoubleSetting("writeBudgetMs", double.Parse(value, CultureInfo.InvariantCulture));
            });
            _runtimeSettingsContainer.Add(MakeFieldRow("writeBudgetMs:", _writeBudgetMsField));

            // --- Security tab ---
            _securitySettingsContainer.Add(MakeHintLabel("Controls network exposure and input validation. Reload save to apply."));

            var savedListenAddress = NormalizeValue(_service.GetUISetting("listenAddress"), "localhost");
            _listenAddressField = MakeTextField(savedListenAddress);
            _listenAddressField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeValue(evt.newValue, "localhost");
                _listenAddressField.SetValueWithoutNotify(value);
                _service.SaveUISetting("listenAddress", value);
            });
            _listenAddressPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_listenAddressPresetBtn, _listenAddressField, ListenAddressChoices));
            _securitySettingsContainer.Add(MakePresetFieldRow("listenAddress:", _listenAddressField, _listenAddressPresetBtn));

            var savedCorsOrigin = _service.GetUISetting("corsOrigin") ?? "";
            _corsOriginField = MakeTextField(savedCorsOrigin);
            _corsOriginField.RegisterValueChangedCallback(evt => _service.SaveUISetting("corsOrigin", evt.newValue ?? ""));
            _corsOriginPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_corsOriginPresetBtn, _corsOriginField, CorsOriginChoices));
            _securitySettingsContainer.Add(MakePresetFieldRow("corsOrigin:", _corsOriginField, _corsOriginPresetBtn));

            var savedMaxBody = NormalizeIntString(_service.GetUISetting("maxBodyBytes"), 1048576, 0);
            _maxBodyBytesField = MakeTextField(savedMaxBody);
            _maxBodyBytesField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeIntString(evt.newValue, 1048576, 0);
                _maxBodyBytesField.SetValueWithoutNotify(value);
                _service.SaveIntSetting("maxBodyBytes", int.Parse(value));
            });
            _securitySettingsContainer.Add(MakeFieldRow("maxBodyBytes:", _maxBodyBytesField));

            _presetPopup = new NineSliceVisualElement();
            _presetPopup.AddToClassList("bg-sub-box--green");
            _presetPopup.style.position = Position.Absolute;
            _presetPopup.style.minWidth = 180;
            _presetPopup.style.paddingTop = 4;
            _presetPopup.style.paddingBottom = 4;
            _presetPopup.style.paddingLeft = 4;
            _presetPopup.style.paddingRight = 4;
            _presetPopup.ToggleDisplayStyle(false);
            _modalPanel.Add(_presetPopup);

            _presetScroll = new ScrollView(ScrollViewMode.Vertical);
            _presetScroll.style.maxHeight = 260;
            _presetScroll.style.minWidth = 172;
            _presetScroll.style.flexGrow = 1;
            _presetPopup.Add(_presetScroll);

            _tooltipPopup = new NineSliceVisualElement();
            _tooltipPopup.AddToClassList("bg-sub-box--green");
            _tooltipPopup.style.position = Position.Absolute;
            _tooltipPopup.style.maxWidth = 320;
            _tooltipPopup.style.paddingTop = 6;
            _tooltipPopup.style.paddingBottom = 6;
            _tooltipPopup.style.paddingLeft = 8;
            _tooltipPopup.style.paddingRight = 8;
            _tooltipPopup.pickingMode = PickingMode.Ignore;
            _tooltipPopup.ToggleDisplayStyle(false);
            _modalOverlay.Add(_tooltipPopup);

            _tooltipLabel = new NineSliceLabel();
            _tooltipLabel.AddToClassList("text--yellow");
            _tooltipLabel.AddToClassList("game-text-normal");
            _tooltipLabel.style.whiteSpace = WhiteSpace.Normal;
            _tooltipLabel.style.maxWidth = 304;
            _tooltipPopup.Add(_tooltipLabel);

            SetSettingsTab(_activeSettingsTab);
        }

        private void ApplySavedWidgetPosition()
        {
            var savedLeft = _service.GetUISetting("widgetLeft");
            var savedTop = _service.GetUISetting("widgetTop");
            if (float.TryParse(savedLeft, out var left) && float.TryParse(savedTop, out var top))
            {
                _widget.style.left = left;
                _widget.style.top = top;
                _widget.style.right = StyleKeyword.Auto;
                _widget.style.bottom = StyleKeyword.Auto;
                _widgetPositionInitialized = true;
            }
            else
            {
                _widget.style.right = 10;
                _widget.style.bottom = 10;
                _widgetPositionInitialized = false;
            }
        }

        private void OnWidgetPointerDown(PointerDownEvent evt)
        {
            if (evt.button != 0)
                return;

            _isWidgetDragging = true;
            _dragPointerId = evt.pointerId;
            _dragStartPointer = new Vector2(evt.position.x, evt.position.y);

            var widgetBounds = _widget.worldBound;
            if (!_widgetPositionInitialized)
            {
                _widget.style.left = widgetBounds.xMin;
                _widget.style.top = widgetBounds.yMin;
                _widget.style.right = StyleKeyword.Auto;
                _widget.style.bottom = StyleKeyword.Auto;
                _widgetPositionInitialized = true;
            }

            _dragStartWidget = new Vector2(_widget.resolvedStyle.left, _widget.resolvedStyle.top);
            _statusBarLabel.CapturePointer(evt.pointerId);
            evt.StopPropagation();
        }

        private void OnWidgetPointerMove(PointerMoveEvent evt)
        {
            if (!_isWidgetDragging || evt.pointerId != _dragPointerId)
                return;

            var pointer = new Vector2(evt.position.x, evt.position.y);
            var delta = pointer - _dragStartPointer;
            var newLeft = _dragStartWidget.x + delta.x;
            var newTop = _dragStartWidget.y + delta.y;
            var root = _widget.parent;
            if (root != null)
            {
                newLeft = Mathf.Clamp(newLeft, 0, Mathf.Max(0, root.resolvedStyle.width - _widget.resolvedStyle.width));
                newTop = Mathf.Clamp(newTop, 0, Mathf.Max(0, root.resolvedStyle.height - _widget.resolvedStyle.height));
            }

            _widget.style.left = newLeft;
            _widget.style.top = newTop;
            _widget.style.right = StyleKeyword.Auto;
            _widget.style.bottom = StyleKeyword.Auto;
            evt.StopPropagation();
        }

        private void OnWidgetPointerUp(PointerUpEvent evt)
        {
            if (!_isWidgetDragging || evt.pointerId != _dragPointerId)
                return;

            _isWidgetDragging = false;
            _statusBarLabel.ReleasePointer(evt.pointerId);
            _service.SaveUISetting("widgetLeft", _widget.resolvedStyle.left.ToString(CultureInfo.InvariantCulture));
            _service.SaveUISetting("widgetTop", _widget.resolvedStyle.top.ToString(CultureInfo.InvariantCulture));
            evt.StopPropagation();
        }

        private void OnOverlayPointerDown(PointerDownEvent evt)
        {
            if (evt.target == _modalOverlay)
            {
                HidePresetMenu();
                HideModal();
            }
        }

        private void ShowModal()
        {
            SetSettingsTab(_activeSettingsTab);
            _modalOverlay.ToggleDisplayStyle(true);
        }

        private void HideModal()
        {
            HidePresetMenu();
            HideTooltip();
            _modalOverlay.ToggleDisplayStyle(false);
        }

        private void ShowAgentTab()
        {
            SetSettingsTab("agent");
        }

        private void ShowRuntimeTab()
        {
            SetSettingsTab("runtime");
        }

        private void ShowSecurityTab()
        {
            SetSettingsTab("security");
        }

        private void SetSettingsTab(string tab)
        {
            _activeSettingsTab = tab == "runtime" ? "runtime" : tab == "security" ? "security" : "agent";

            if (_agentSettingsContainer != null)
                _agentSettingsContainer.ToggleDisplayStyle(_activeSettingsTab == "agent");
            if (_runtimeSettingsContainer != null)
                _runtimeSettingsContainer.ToggleDisplayStyle(_activeSettingsTab == "runtime");
            if (_securitySettingsContainer != null)
                _securitySettingsContainer.ToggleDisplayStyle(_activeSettingsTab == "security");

            if (_agentTabBtn != null)
                _agentTabBtn.SetEnabled(_activeSettingsTab != "agent");
            if (_startupTabBtn != null)
                _startupTabBtn.SetEnabled(_activeSettingsTab != "runtime");
            if (_securityTabBtn != null)
                _securityTabBtn.SetEnabled(_activeSettingsTab != "security");

            HidePresetMenu();
            HideTooltip();
        }

        private void OnLaunchClicked()
        {
            HidePresetMenu();
            // In request mode the Launch button doubles as Send: post the
            // textarea contents as the next one-shot prompt before flipping
            // the gate, so the connector finds the slot already filled when
            // it heartbeats next.
            if (_currentMode == ModeRequest && _textareaField != null && !string.IsNullOrWhiteSpace(_textareaField.value))
            {
                var prompt = _textareaField.value;
                PostJsonAsync("/api/agent/request", new JObject { ["prompt"] = prompt });
                _textareaField.SetValueWithoutNotify("");
                _lastTextareaSavedValue = "";
                _requestDraft = "";
            }
            PostJsonAsync("/api/ready", new JObject { ["ready"] = true });
            TimberbotLog.Info("panel: launch (ready=true)");
        }

        private void OnStopClicked()
        {
            PostJsonAsync("/api/ready", new JObject { ["ready"] = false });
            TimberbotLog.Info("panel: stop (ready=false)");
        }

        private void OnMinimizeClicked()
        {
            _widgetMinimized = !_widgetMinimized;
            _widgetButtonRow.style.display = _widgetMinimized ? DisplayStyle.None : DisplayStyle.Flex;
            _widgetMinimizeBtn.text = _widgetMinimized ? "+" : "-";
            _service.SaveUISetting("widgetMinimized", _widgetMinimized ? "true" : "false");

            if (_widgetMinimized)
            {
                _preMinimizeLeft = _widget.resolvedStyle.left;
                _preMinimizeTop = _widget.resolvedStyle.top;
                _hadPreMinimizePosition = _widgetPositionInitialized;
                _widget.style.left = StyleKeyword.Auto;
                _widget.style.top = StyleKeyword.Auto;
                _widget.style.right = 10;
                _widget.style.bottom = 10;
            }
            else if (_hadPreMinimizePosition)
            {
                _widget.style.left = _preMinimizeLeft;
                _widget.style.top = _preMinimizeTop;
                _widget.style.right = StyleKeyword.Auto;
                _widget.style.bottom = StyleKeyword.Auto;
            }
        }

        private void OnModalLaunchClicked()
        {
            OnLaunchClicked();
            HideModal();
        }

        private void TogglePresetMenu(VisualElement anchor, TextField targetField, string[][] choices)
        {
            if (_presetPopupAnchor == anchor && _presetPopup.resolvedStyle.display != DisplayStyle.None)
            {
                HidePresetMenu();
                return;
            }

            ShowPresetMenu(anchor, targetField, choices);
        }

        private void ShowPresetMenu(VisualElement anchor, TextField targetField, string[][] choices)
        {
            HideTooltip();
            _presetScroll.Clear();
            _presetPopupAnchor = anchor;

            foreach (var choice in choices)
            {
                var label = choice[0];
                var value = choice.Length > 1 ? choice[1] : choice[0];
                _presetScroll.Add(MakePresetOptionButton(label, () =>
                {
                    targetField.value = value;
                    HidePresetMenu();
                }));
            }

            const float optionHeight = 26f;
            const float popupPadding = 8f;
            const float popupWidth = 220f;
            const float panelMargin = 12f;
            const float offsetY = 4f;

            var panelBounds = _modalPanel.worldBound;
            var anchorBounds = anchor.worldBound;
            var desiredHeight = choices.Length * optionHeight + popupPadding;
            var maxHeight = Mathf.Max(optionHeight + popupPadding, panelBounds.height - (panelMargin * 2f));
            var popupHeight = Mathf.Min(desiredHeight, maxHeight);

            var preferredLeft = anchorBounds.xMin - panelBounds.xMin;
            var left = Mathf.Clamp(preferredLeft, panelMargin, panelBounds.width - popupWidth - panelMargin);

            var preferredTop = anchorBounds.yMax - panelBounds.yMin + offsetY;
            var top = preferredTop;
            if (top + popupHeight > panelBounds.height - panelMargin)
                top = anchorBounds.yMin - panelBounds.yMin - popupHeight - offsetY;
            top = Mathf.Clamp(top, panelMargin, panelBounds.height - popupHeight - panelMargin);

            _presetPopup.style.width = popupWidth;
            _presetPopup.style.height = popupHeight;
            _presetPopup.style.left = left;
            _presetPopup.style.top = top;
            _presetPopup.ToggleDisplayStyle(true);
            _presetPopup.BringToFront();
        }

        private void HidePresetMenu()
        {
            if (_presetPopup == null)
                return;

            _presetScroll.Clear();
            _presetPopup.ToggleDisplayStyle(false);
            _presetPopupAnchor = null;
        }

        private void QueueTooltip(VisualElement anchor, string text, Vector2 pointerPosition)
        {
            if (_tooltipPopup == null || string.IsNullOrWhiteSpace(text))
                return;

            _pendingTooltipAnchor = anchor;
            _pendingTooltipText = text;
            _tooltipPointerPosition = pointerPosition;
            var requestId = ++_tooltipRequestId;
            _modalOverlay.schedule.Execute(() =>
            {
                if (requestId != _tooltipRequestId || _pendingTooltipAnchor == null || string.IsNullOrWhiteSpace(_pendingTooltipText))
                    return;

                ShowTooltip(_pendingTooltipAnchor, _pendingTooltipText);
            }).StartingIn(200);
        }

        private void ShowTooltip(VisualElement anchor, string text)
        {
            if (_tooltipPopup == null || _tooltipLabel == null || anchor == null || string.IsNullOrWhiteSpace(text))
                return;

            _tooltipAnchor = anchor;
            _tooltipLabel.text = text;
            _tooltipPopup.ToggleDisplayStyle(true);
            _tooltipPopup.BringToFront();
            PositionTooltip(anchor);
        }

        private void PositionTooltip(VisualElement anchor)
        {
            if (_tooltipPopup == null || anchor == null)
                return;

            var overlayBounds = _modalOverlay.worldBound;
            var anchorBounds = anchor.worldBound;
            var pointerX = _tooltipPointerPosition.x;
            var pointerY = _tooltipPointerPosition.y;
            if (pointerX <= 0f && pointerY <= 0f)
            {
                pointerX = anchorBounds.xMax;
                pointerY = anchorBounds.center.y;
            }

            const float offset = 12f;
            const float margin = 12f;
            var width = Mathf.Max(180f, _tooltipPopup.resolvedStyle.width);
            var height = Mathf.Max(40f, _tooltipPopup.resolvedStyle.height);

            var left = pointerX - overlayBounds.xMin + offset;
            var top = pointerY - overlayBounds.yMin - (height * 0.5f);

            if (left + width > overlayBounds.width - margin)
                left = anchorBounds.xMin - overlayBounds.xMin - width - offset;
            if (left < margin)
                left = margin;

            if (top + height > overlayBounds.height - margin)
                top = overlayBounds.height - height - margin;
            if (top < margin)
                top = margin;

            _tooltipPopup.style.left = left;
            _tooltipPopup.style.top = top;
        }

        private void HideTooltip()
        {
            _tooltipRequestId++;
            _pendingTooltipAnchor = null;
            _pendingTooltipText = null;
            _tooltipAnchor = null;
            if (_tooltipPopup != null)
                _tooltipPopup.ToggleDisplayStyle(false);
        }

        private void RegisterTooltipHandlers(VisualElement row, string tooltipText)
        {
            if (string.IsNullOrWhiteSpace(tooltipText))
                return;

            row.RegisterCallback<MouseEnterEvent>(evt =>
            {
                var pointer = new Vector2(evt.mousePosition.x, evt.mousePosition.y);
                QueueTooltip(row, tooltipText, pointer);
            });
            row.RegisterCallback<MouseLeaveEvent>(evt => HideTooltip());
            row.RegisterCallback<PointerMoveEvent>(evt =>
            {
                _tooltipPointerPosition = new Vector2(evt.position.x, evt.position.y);
                if (_tooltipAnchor == row && _tooltipPopup != null && _tooltipPopup.resolvedStyle.display != DisplayStyle.None)
                    PositionTooltip(row);
            });
        }

        private static string NormalizeValue(string value, string fallback) => TimberbotPure.NormalizeValue(value, fallback);

        private static string NormalizeBoolString(string value, bool fallback) => TimberbotPure.NormalizeBoolString(value, fallback);

        private static string NormalizeIntString(string value, int fallback, int minValue) => TimberbotPure.NormalizeIntString(value, fallback, minValue);

        private static string NormalizeDoubleString(string value, double fallback, double minValue) => TimberbotPure.NormalizeDoubleString(value, fallback, minValue);

        private static string NormalizeMode(string raw) => TimberbotPure.NormalizeMode(raw);

        // Swap the textarea contents when the mode changes. Autonomous mode
        // shows the persisted goal (so the player can edit it); request mode
        // restores the in-progress prompt draft if there is one, otherwise
        // starts empty. The draft is cleared only on Launch.
        private void SyncTextareaForMode(string mode, JObject state)
        {
            if (_textareaField == null) return;
            if (mode == ModeRequest)
            {
                _textareaField.SetValueWithoutNotify(_requestDraft ?? "");
                _lastTextareaSavedValue = "";
            }
            else
            {
                var goal = state?.Value<string>("goal") ?? _service.GetUISetting("agentGoal") ?? DefaultGoal;
                _textareaField.SetValueWithoutNotify(goal);
                _lastTextareaSavedValue = goal;
            }
        }

        // Fire-and-forget JSON POST. Errors land in the log; the UI doesn't
        // wait for the response because the state poll picks up the change on
        // the next tick. ConfigureAwait(false) keeps us off the main thread.
        private void PostJsonAsync(string path, JObject body)
        {
            var payload = body?.ToString(Newtonsoft.Json.Formatting.None) ?? "{}";
            _ = Task.Run(async () =>
            {
                try
                {
                    using var content = new StringContent(payload, Encoding.UTF8, "application/json");
                    using var req = NewRequest(HttpMethod.Post, path);
                    req.Content = content;
                    using var resp = await _http.SendAsync(req).ConfigureAwait(false);
                    if (!resp.IsSuccessStatusCode)
                    {
                        var bodyText = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
                        TimberbotLog.Info($"panel.post {path} -> {(int)resp.StatusCode}: {bodyText}");
                    }
                }
                catch (Exception ex)
                {
                    TimberbotLog.Info($"panel.post {path} failed: {ex.GetType().Name}: {ex.Message}");
                }
            });
        }

        // Build an HttpRequestMessage with the mod's bearer token attached when
        // settings.json sets `authToken`. The widget is a regular HTTP client of
        // its own server, so the auth middleware applies to it too — without
        // this header the panel gets 401'd on every /api/agent/state poll and
        // every /api/ready POST, breaking the Launch button entirely.
        private HttpRequestMessage NewRequest(HttpMethod method, string path)
        {
            var req = new HttpRequestMessage(method, _baseUrl + path);
            var token = _service.AuthToken;
            if (!string.IsNullOrEmpty(token))
            {
                req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
            }
            return req;
        }

        private static VisualElement MakeSeparator()
        {
            var sep = new VisualElement();
            sep.style.height = 1;
            sep.style.backgroundColor = new Color(0.4f, 0.36f, 0.28f);
            sep.style.marginTop = 4;
            sep.style.marginBottom = 4;
            return sep;
        }

        private static NineSliceLabel MakeLabel(string text)
        {
            var label = new NineSliceLabel { text = text };
            label.AddToClassList("text--yellow");
            label.AddToClassList("game-text-normal");
            label.style.overflow = Overflow.Hidden;
            label.style.whiteSpace = WhiteSpace.NoWrap;
            label.style.marginBottom = 2;
            return label;
        }

        private static NineSliceLabel MakeSectionLabel(string text)
        {
            var label = new NineSliceLabel { text = text };
            label.AddToClassList("text--yellow");
            label.AddToClassList("game-text-normal");
            label.AddToClassList("text--bold");
            label.style.marginTop = 2;
            label.style.marginBottom = 6;
            return label;
        }

        private static NineSliceLabel MakeHintLabel(string text)
        {
            var label = new NineSliceLabel { text = text };
            label.AddToClassList("text--green");
            label.AddToClassList("game-text-normal");
            label.style.whiteSpace = WhiteSpace.Normal;
            label.style.marginBottom = 6;
            return label;
        }

        private static NineSliceTextField MakeTextField(string defaultValue)
        {
            var field = new NineSliceTextField();
            field.AddToClassList("text-field");
            field.value = defaultValue;
            field.style.height = 22;
            field.style.flexGrow = 1;
            return field;
        }

        private static NineSliceButton MakePresetButton(string text, System.Action onClick)
        {
            var btn = new NineSliceButton { text = text };
            btn.AddToClassList("button-game");
            btn.AddToClassList("game-text-normal");
            btn.style.width = 22;
            btn.style.height = 22;
            btn.style.paddingLeft = 0;
            btn.style.paddingRight = 0;
            btn.clicked += onClick;
            return btn;
        }

        private static NineSliceButton MakePresetOptionButton(string text, System.Action onClick)
        {
            var btn = new NineSliceButton { text = text };
            btn.AddToClassList("button-game");
            btn.AddToClassList("game-text-normal");
            btn.style.height = 24;
            btn.style.marginBottom = 2;
            btn.clicked += onClick;
            return btn;
        }

        private static NineSliceButton MakeGameButton(string text, System.Action onClick)
        {
            var btn = new NineSliceButton { text = text };
            btn.AddToClassList("button-game");
            btn.AddToClassList("game-text-normal");
            btn.style.width = 80;
            btn.style.height = 26;
            btn.style.paddingTop = 2;
            btn.style.paddingBottom = 2;
            btn.style.paddingLeft = 10;
            btn.style.paddingRight = 10;
            btn.clicked += onClick;
            return btn;
        }

        private VisualElement MakeFieldRow(string labelText, VisualElement field)
        {
            var row = new VisualElement();
            row.style.flexDirection = FlexDirection.Row;
            row.style.alignItems = Align.Center;
            row.style.marginBottom = 6;

            var lbl = new NineSliceLabel { text = labelText };
            lbl.AddToClassList("text--yellow");
            lbl.AddToClassList("game-text-normal");
            lbl.style.width = 150;
            row.Add(lbl);

            field.style.flexGrow = 1;
            row.Add(field);
            if (SettingTooltips.TryGetValue(labelText, out var tooltipText))
                RegisterTooltipHandlers(row, tooltipText);
            return row;
        }

        private VisualElement MakePresetFieldRow(string labelText, TextField field, NineSliceButton button)
        {
            var row = MakeFieldRow(labelText, field);
            button.style.marginLeft = 4;
            row.Add(button);
            return row;
        }

        private void BuildConsole()
        {
            _consoleRoot = new VisualElement();
            _consoleRoot.style.position = Position.Absolute;
            _consoleRoot.style.bottom = 280;
            _consoleRoot.style.left = 10;
            _consoleRoot.style.width = 380;
            _consoleRoot.style.height = 180;
            _consoleRoot.style.paddingTop = 4;
            _consoleRoot.style.paddingBottom = 4;
            _consoleRoot.style.paddingLeft = 8;
            _consoleRoot.style.paddingRight = 8;
            
            // Premium Glassy HUD style
            _consoleRoot.style.backgroundColor = new Color(0f, 0.02f, 0f, 0.75f);
            _consoleRoot.style.borderLeftWidth = 1;
            _consoleRoot.style.borderRightWidth = 1;
            _consoleRoot.style.borderTopWidth = 1;
            _consoleRoot.style.borderBottomWidth = 1;
            _consoleRoot.style.borderLeftColor = new Color(0.3f, 0.5f, 0.3f, 0.4f);
            _consoleRoot.style.borderRightColor = new Color(0.3f, 0.5f, 0.3f, 0.4f);
            _consoleRoot.style.borderTopColor = new Color(0.3f, 0.5f, 0.3f, 0.4f);
            _consoleRoot.style.borderBottomColor = new Color(0.3f, 0.5f, 0.3f, 0.4f);
            _consoleRoot.style.borderTopLeftRadius = 6;
            _consoleRoot.style.borderTopRightRadius = 6;
            _consoleRoot.style.borderBottomLeftRadius = 6;
            _consoleRoot.style.borderBottomRightRadius = 6;

            // Containers for layout state switching
            var expandedBox = new VisualElement();
            expandedBox.style.flexGrow = 1;
            expandedBox.style.display = DisplayStyle.Flex;

            var collapsedBox = new VisualElement();
            collapsedBox.style.flexGrow = 1;
            collapsedBox.style.alignItems = Align.Center;
            collapsedBox.style.justifyContent = Justify.Center;
            collapsedBox.style.display = DisplayStyle.None;

            _consoleRoot.Add(expandedBox);
            _consoleRoot.Add(collapsedBox);

            // Layout toggler logic
            System.Action refreshLayout = () =>
            {
                bool hide = _consoleCollapsed;
                expandedBox.style.display = hide ? DisplayStyle.None : DisplayStyle.Flex;
                collapsedBox.style.display = hide ? DisplayStyle.Flex : DisplayStyle.None;

                if (hide)
                {
                    _consoleRoot.style.width = 36;
                    _consoleRoot.style.height = 36;
                    _consoleRoot.style.paddingLeft = 0;
                    _consoleRoot.style.paddingRight = 0;
                    _consoleRoot.style.paddingTop = 0;
                    _consoleRoot.style.paddingBottom = 0;
                }
                else
                {
                    _consoleRoot.style.width = 380;
                    _consoleRoot.style.height = 180;
                    _consoleRoot.style.paddingLeft = 8;
                    _consoleRoot.style.paddingRight = 8;
                    _consoleRoot.style.paddingTop = 4;
                    _consoleRoot.style.paddingBottom = 4;
                }
            };

            // ==========================================
            // 1. BUILD EXPANDED VIEW
            // ==========================================
            var titleContainer = new VisualElement();
            titleContainer.style.flexDirection = FlexDirection.Row;
            titleContainer.style.justifyContent = Justify.SpaceBetween;
            titleContainer.style.alignItems = Align.Center;
            titleContainer.style.marginBottom = 6;
            titleContainer.style.paddingBottom = 2;
            titleContainer.style.borderBottomWidth = 1;
            titleContainer.style.borderBottomColor = new Color(0.3f, 0.5f, 0.3f, 0.2f);
            expandedBox.Add(titleContainer);

            var titleLabel = new Label("Timberbot Action Console");
            titleLabel.AddToClassList("text--yellow");
            titleLabel.AddToClassList("game-text-normal");
            titleLabel.AddToClassList("text--bold");
            titleLabel.style.fontSize = 10;
            titleLabel.style.flexGrow = 1; // allow maximum clickable area for drag
            titleContainer.Add(titleLabel);

            var minimizeBtn = new Label("[-]");
            minimizeBtn.AddToClassList("text--yellow");
            minimizeBtn.AddToClassList("text--bold");
            minimizeBtn.style.fontSize = 10;
            minimizeBtn.style.marginLeft = 4;
            minimizeBtn.RegisterCallback<ClickEvent>(evt =>
            {
                _consoleCollapsed = true;
                refreshLayout();
                evt.StopPropagation();
            });
            titleContainer.Add(minimizeBtn);

            _consoleScroll = new ScrollView(ScrollViewMode.Vertical);
            _consoleScroll.style.flexGrow = 1;
            expandedBox.Add(_consoleScroll);

            // ==========================================
            // 2. BUILD COLLAPSED VIEW (Tiny Square Button)
            // ==========================================
            var expandIcon = new Label("[+]");
            expandIcon.AddToClassList("text--yellow");
            expandIcon.AddToClassList("game-text-normal");
            expandIcon.AddToClassList("text--bold");
            expandIcon.style.fontSize = 12;
            expandIcon.style.marginTop = 0;
            collapsedBox.Add(expandIcon);

            // ==========================================
            // 3. INJECT REUSABLE DRAG MECHANICS
            // ==========================================
            System.Action<VisualElement, System.Action> bindDraggable = (target, onClick) =>
            {
                Vector2 anchorRootPos = Vector2.zero;
                Vector2 anchorMousePos = Vector2.zero;
                float dragDistSq = 0f;

                target.RegisterCallback<PointerDownEvent>(evt =>
                {
                    anchorRootPos = new Vector2(_consoleRoot.resolvedStyle.left, _consoleRoot.resolvedStyle.top);
                    anchorMousePos = evt.position;
                    dragDistSq = 0f;
                    _consoleIsDragging = true;
                    target.CapturePointer(evt.pointerId);
                    evt.StopPropagation();
                });

                target.RegisterCallback<PointerMoveEvent>(evt =>
                {
                    if (!_consoleIsDragging) return;
                    Vector2 delta = (Vector2)evt.position - anchorMousePos;
                    dragDistSq = delta.sqrMagnitude;
                    
                    _consoleRoot.style.bottom = StyleKeyword.Null;
                    _consoleRoot.style.left = anchorRootPos.x + delta.x;
                    _consoleRoot.style.top = anchorRootPos.y + delta.y;
                    evt.StopPropagation();
                });

                target.RegisterCallback<PointerUpEvent>(evt =>
                {
                    if (_consoleIsDragging)
                    {
                        _consoleIsDragging = false;
                        target.ReleasePointer(evt.pointerId);
                        evt.StopPropagation();

                        // Fire click logic ONLY if movement is minimal (<5px radius)
                        if (onClick != null && dragDistSq < 25f)
                        {
                            onClick();
                        }
                    }
                });
            };

            // Apply drag logic. The collapsed box expands ONLY on actual stationary click.
            bindDraggable(titleLabel, null);
            bindDraggable(collapsedBox, () =>
            {
                _consoleCollapsed = false;
                refreshLayout();
            });
        }

        private void UpdateConsoleVisibility()
        {
            if (_consoleRoot != null)
            {
                _consoleRoot.ToggleDisplayStyle(_service.ActionLoggingEnabled);
            }
        }

        private void HandleActionLog(string message)
        {
            if (_consoleScroll == null) return;

            var row = new Label($"[{System.DateTime.Now:HH:mm:ss}] {message}");
            row.style.color = Color.white;
            row.style.fontSize = 11;
            row.style.whiteSpace = WhiteSpace.Normal;
            row.style.marginBottom = 2;
            
            _consoleScroll.Add(row);

            if (_consoleScroll.childCount > 40)
                _consoleScroll.RemoveAt(0);

            _consoleScroll.schedule.Execute(() => {
                _consoleScroll.verticalScroller.value = _consoleScroll.verticalScroller.highValue;
            }).StartingIn(50);
        }
    }
}






