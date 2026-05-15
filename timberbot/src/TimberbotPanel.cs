// TimberbotPanel.cs. In-game UI for agent start/stop/status.

using System.Collections.Generic;
using System.Globalization;
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
        private Label _statusBarLabel;
        private NineSliceButton _widgetStartBtn;
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

        private TextField _binaryField;
        private NineSliceButton _binaryPresetBtn;
        private TextField _goalField;
        private TextField _debugEndpointField;
        private NineSliceButton _debugEndpointPresetBtn;
        private TextField _httpPortField;
        private TextField _webhooksEnabledField;
        private NineSliceButton _webhooksEnabledPresetBtn;
        private TextField _webhookBatchMsField;
        private TextField _webhookCircuitBreakerField;
        private TextField _webhookMaxPendingEventsField;
        private TextField _writeBudgetMsField;

        // security tab fields
        private TextField _listenAddressField;
        private NineSliceButton _listenAddressPresetBtn;
        private TextField _corsOriginField;
        private NineSliceButton _corsOriginPresetBtn;
        private TextField _webhookValidateUrlsField;
        private NineSliceButton _webhookValidateUrlsPresetBtn;
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

        // Backend list mirrors the names registered in the Python `tbot agent`
        // CLI. Model/effort defaults now live on each backend in Python; the
        // in-game panel only chooses which backend to launch.
        private static readonly string[][] BinaryChoices = new[]
        {
            new[] { "claude", "claude" },
            new[] { "codex", "codex" },
            new[] { "opencode", "opencode" },
            new[] { "custom", "custom" },
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

        private const string DefaultBinary = "claude";

        private static readonly Dictionary<string, string> SettingTooltips = new Dictionary<string, string>
        {
            ["Backend:"] = "Which agent backend `tbot agent run` launches. Built-in: claude, codex, opencode. Use 'custom' to invoke a backend defined in ~/.config/timberbot/config.toml.",
            ["Goal:"] = "Initial task sent to the agent after it prints the boot report. The merged system prompt also includes the guide and current colony state.",
            ["actionLoggingEnabled:"] = "Logs agent write/placement actions to the in-game console panel. Takes effect immediately.",
            ["debugEndpointEnabled:"] = "Enables debug and benchmark endpoints such as /api/debug and /api/benchmark. Reload save to apply.",
            ["httpPort:"] = "HTTP server port Timberbot listens on. The Python client reads this by default from settings.json. Reload save to apply.",
            ["webhooksEnabled:"] = "Turns outgoing webhook event delivery on or off. Reload save to apply.",
            ["webhookBatchMs:"] = "Webhook batching window in milliseconds. Use 0 for immediate delivery instead of batching. Reload save to apply.",
            ["webhookCircuitBreaker:"] = "Number of consecutive webhook delivery failures before Timberbot disables webhook sending. Reload save to apply.",
            ["webhookMaxPendingEvents:"] = "Per-webhook cap for queued event payloads while delivery is in flight or failing. Oldest queued events are dropped when the cap is reached. Reload save to apply.",
            ["writeBudgetMs:"] = "Per-frame main-thread time budget for queued write jobs. Higher values process writes faster but use more frame time. Reload save to apply.",
            ["listenAddress:"] = "Network address the HTTP server binds to. 'localhost' (default) = local only. '+' = all interfaces (LAN access). Reload save to apply.",
            ["corsOrigin:"] = "Allowed CORS origin for browser requests. Empty = auto (localhost only). '*' = any origin (less secure). Reload save to apply.",
            ["webhookValidateUrls:"] = "When true, webhook URLs are validated: must be http/https and must not resolve to private/internal IP addresses. Set false to allow any URL. Reload save to apply.",
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

            float now = Time.realtimeSinceStartup;
            if (now - _lastUpdate < 0.5f)
                return;
            _lastUpdate = now;

            var agent = _service.Agent;
            if (agent == null)
                return;

            var status = agent.CurrentStatus;
            var statusText = FormatStatus(agent);
            var running = status == AgentStatus.GatheringState || status == AgentStatus.Interactive;

            _statusBarLabel.text = "Timberbot API - " + statusText;

            _widgetStartBtn.SetEnabled(!running);
            _widgetStopBtn.SetEnabled(running);
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

            _statusBarLabel = new NineSliceLabel { text = "Timberbot API - Idle" };
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

            _widgetButtonRow = new VisualElement();
            _widgetButtonRow.style.flexDirection = FlexDirection.Row;
            _widgetButtonRow.style.justifyContent = Justify.Center;
            _widgetButtonRow.style.alignItems = Align.Center;
            _widgetButtonRow.style.marginTop = 4;
            _widgetButtonRow.style.display = _widgetMinimized ? DisplayStyle.None : DisplayStyle.Flex;

            _widgetStartBtn = MakeGameButton("Start", OnStartClicked);
            _widgetStartBtn.style.width = 58;
            _widgetStartBtn.style.marginRight = 4;
            _widgetButtonRow.Add(_widgetStartBtn);

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

            var savedBinary = NormalizeValue(_service.GetUISetting("agentBinary"), DefaultBinary);
            var savedGoal = _service.GetUISetting("agentGoal") ?? "reach 50 beavers with 77 well-being";
            var savedActionLoggingEnabled = NormalizeBoolString(_service.GetUISetting("actionLoggingEnabled"), true);
            var savedDebugEndpointEnabled = NormalizeBoolString(_service.GetUISetting("debugEndpointEnabled"), false);
            var savedHttpPort = NormalizeValue(_service.GetUISetting("httpPort"), "8085");
            var savedWebhooksEnabled = NormalizeBoolString(_service.GetUISetting("webhooksEnabled"), true);
            var savedWebhookBatchMs = NormalizeValue(_service.GetUISetting("webhookBatchMs"), "200");
            var savedWebhookCircuitBreaker = NormalizeValue(_service.GetUISetting("webhookCircuitBreaker"), "30");
            var savedWebhookMaxPendingEvents = NormalizeValue(_service.GetUISetting("webhookMaxPendingEvents"), "1000");
            var savedWriteBudgetMs = NormalizeValue(_service.GetUISetting("writeBudgetMs"), "1.0");

            _binaryField = MakeTextField(savedBinary);
            _binaryField.RegisterValueChangedCallback(evt =>
            {
                var binary = NormalizeValue(evt.newValue, DefaultBinary);
                _service.SaveUISetting("agentBinary", binary);
            });
            _binaryPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_binaryPresetBtn, _binaryField, BinaryChoices));
            _agentSettingsContainer.Add(MakePresetFieldRow("Backend:", _binaryField, _binaryPresetBtn));

            _goalField = MakeTextField(savedGoal);
            _goalField.multiline = true;
            _goalField.style.height = 80;
            _goalField.style.flexShrink = 1;
            _goalField.style.maxWidth = 240;
            _goalField.style.whiteSpace = WhiteSpace.Normal;
            // ensure inner text element wraps too
            var goalInput = _goalField.Q("unity-text-input");
            if (goalInput != null)
                goalInput.style.whiteSpace = WhiteSpace.Normal;
            _goalField.RegisterValueChangedCallback(evt => _service.SaveUISetting("agentGoal", evt.newValue));
            _agentSettingsContainer.Add(MakeFieldRow("Goal:", _goalField));

            var agentActionRow = new VisualElement();
            agentActionRow.style.flexDirection = FlexDirection.Row;
            agentActionRow.style.justifyContent = Justify.FlexEnd;
            agentActionRow.style.marginTop = 6;
            var modalStartBtn = MakeGameButton("Start", OnModalStartClicked);
            modalStartBtn.style.width = 70;
            agentActionRow.Add(modalStartBtn);
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

            _webhooksEnabledField = MakeTextField(savedWebhooksEnabled);
            _webhooksEnabledField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeBoolString(evt.newValue, true);
                _webhooksEnabledField.SetValueWithoutNotify(value);
                _service.SaveBoolSetting("webhooksEnabled", value == "true");
            });
            _webhooksEnabledPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_webhooksEnabledPresetBtn, _webhooksEnabledField, BoolChoices));
            _runtimeSettingsContainer.Add(MakePresetFieldRow("webhooksEnabled:", _webhooksEnabledField, _webhooksEnabledPresetBtn));

            _webhookBatchMsField = MakeTextField(savedWebhookBatchMs);
            _webhookBatchMsField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeIntString(evt.newValue, 200, 0);
                _webhookBatchMsField.SetValueWithoutNotify(value);
                _service.SaveIntSetting("webhookBatchMs", int.Parse(value));
            });
            _runtimeSettingsContainer.Add(MakeFieldRow("webhookBatchMs:", _webhookBatchMsField));

            _webhookCircuitBreakerField = MakeTextField(savedWebhookCircuitBreaker);
            _webhookCircuitBreakerField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeIntString(evt.newValue, 30, 1);
                _webhookCircuitBreakerField.SetValueWithoutNotify(value);
                _service.SaveIntSetting("webhookCircuitBreaker", int.Parse(value));
            });
            _runtimeSettingsContainer.Add(MakeFieldRow("webhookCircuitBreaker:", _webhookCircuitBreakerField));

            _webhookMaxPendingEventsField = MakeTextField(savedWebhookMaxPendingEvents);
            _webhookMaxPendingEventsField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeIntString(evt.newValue, 1000, 1);
                _webhookMaxPendingEventsField.SetValueWithoutNotify(value);
                _service.SaveIntSetting("webhookMaxPendingEvents", int.Parse(value));
            });
            _runtimeSettingsContainer.Add(MakeFieldRow("webhookMaxPendingEvents:", _webhookMaxPendingEventsField));

            _writeBudgetMsField = MakeTextField(savedWriteBudgetMs);
            _writeBudgetMsField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeDoubleString(evt.newValue, 1.0, 0.001);
                _writeBudgetMsField.SetValueWithoutNotify(value);
                _service.SaveDoubleSetting("writeBudgetMs", double.Parse(value, System.Globalization.CultureInfo.InvariantCulture));
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

            var savedWebhookValidate = NormalizeBoolString(_service.GetUISetting("webhookValidateUrls"), true);
            _webhookValidateUrlsField = MakeTextField(savedWebhookValidate);
            _webhookValidateUrlsField.RegisterValueChangedCallback(evt =>
            {
                var value = NormalizeBoolString(evt.newValue, true);
                _webhookValidateUrlsField.SetValueWithoutNotify(value);
                _service.SaveBoolSetting("webhookValidateUrls", value == "true");
            });
            _webhookValidateUrlsPresetBtn = MakePresetButton("v", () => TogglePresetMenu(_webhookValidateUrlsPresetBtn, _webhookValidateUrlsField, BoolChoices));
            _securitySettingsContainer.Add(MakePresetFieldRow("webhookValidateUrls:", _webhookValidateUrlsField, _webhookValidateUrlsPresetBtn));

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
            _service.SaveUISetting("widgetLeft", _widget.resolvedStyle.left.ToString(System.Globalization.CultureInfo.InvariantCulture));
            _service.SaveUISetting("widgetTop", _widget.resolvedStyle.top.ToString(System.Globalization.CultureInfo.InvariantCulture));
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

        private void OnStartClicked()
        {
            var agent = _service.Agent;
            if (agent == null)
                return;

            var binary = NormalizeValue(_binaryField.value, "claude");
            var goal = _goalField.value;

            // Model, effort, and custom command template live in
            // ~/.config/timberbot/config.toml (via the Python agent backends)
            // since PR 4. Pass them as empty so each backend's own defaults
            // kick in; the player can still override via `tbot agent run`.
            agent.Start(binary, model: null, effort: null, timeout: 120, goal: goal);
            TimberbotLog.Info($"panel: started agent binary={binary}");
            HidePresetMenu();
        }

        private void OnStopClicked()
        {
            _service.Agent?.Stop();
            TimberbotLog.Info("panel: stopped agent");
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

        private void OnModalStartClicked()
        {
            OnStartClicked();
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

        private static string FormatStatus(TimberbotAgent agent)
        {
            switch (agent.CurrentStatus)
            {
                case AgentStatus.Idle: return "Idle";
                case AgentStatus.Done: return "Done";
                case AgentStatus.Error: return "Error";
                case AgentStatus.GatheringState: return "Loading...";
                case AgentStatus.Interactive: return "Running";
                default: return agent.CurrentStatus.ToString();
            }
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






