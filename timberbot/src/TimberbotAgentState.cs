// TimberbotAgentState.cs. Thread-safe state container for the widget/connector
// HTTP surface introduced in the mod ↔ connector architecture rework.
//
// State partitioning:
//   PERSISTED (state.json) -- survives game reload:
//     mode      : "autonomous" | "request" (default "request")
//     goal      : free-form text describing the agent's objective
//     lastError : last fatal error reported by the connector, or null
//
//   EPHEMERAL (memory-only, reset on every load):
//     ready                 : false until the player presses Launch in the widget
//     pendingRequest        : single-slot {id, prompt} waiting to be acked
//     lastAckedRequestId    : monotonic ack counter for the pending slot
//     agentStatus           : opaque connector-reported status string
//     lastHeartbeatUtc      : sliding window WS-side heartbeat tracking
//
// All public mutators lock a single object so handlers running on the HTTP
// listener thread (e.g. /api/agent/state) and the Unity main thread (write-job
// drains) see a consistent snapshot. The container itself has no Unity or
// Timberborn dependency so it can be linked straight into the xUnit test
// assembly.
//
// State broadcast: mutating methods (SetReady, SetMode, SetGoal,
// SetPendingRequest/EnqueueRequest, ClearPendingIfAcked, Heartbeat) emit a
// `Changed` event AFTER releasing the lock so the WS broadcaster can fan-out
// without stalling concurrent writers / readers. The event delivers the
// current `ToStateResponseJson()` payload so subscribers do not need to
// re-snapshot the container.
using System;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Timberbot
{
    public class TimberbotAgentState
    {
        public const string ModeAutonomous = "autonomous";
        public const string ModeRequest = "request";
        public const string DefaultMode = ModeRequest;

        // Sliding window for tracking WS-side connector liveness. Anything
        // older indicates the connector has gone away; the WS server uses its
        // own per-connection close detection so the field is informational
        // only.
        public static readonly TimeSpan HeartbeatTimeout = TimeSpan.FromSeconds(30);

        // Raised AFTER any state mutation that should be visible to WS
        // subscribers. Payload is the rendered `ToStateResponseJson()` for the
        // post-change snapshot. Fired outside the container lock so handlers
        // are free to do awaits / I/O without back-pressuring the mutator.
        public event Action<string> Changed;

        private readonly object _lock = new object();

        // Persisted fields.
        private string _mode = DefaultMode;
        private string _goal = "";
        private string _lastError;

        // Ephemeral fields.
        private bool _ready;
        private int _pendingRequestId;
        private string _pendingRequestPrompt;
        private bool _hasPendingRequest;
        private long _lastAckedRequestId;
        private string _agentStatus = "idle";
        private DateTime _lastHeartbeatUtc = DateTime.MinValue;
        private int _nextRequestId;

        public string Mode { get { lock (_lock) return _mode; } }
        public string Goal { get { lock (_lock) return _goal; } }
        public string LastError { get { lock (_lock) return _lastError; } }
        public bool Ready { get { lock (_lock) return _ready; } }
        public string AgentStatus { get { lock (_lock) return _agentStatus; } }
        public long LastAckedRequestId { get { lock (_lock) return _lastAckedRequestId; } }

        // Tuple-style snapshot for tests; avoids exposing the lock itself.
        public (int Id, string Prompt)? PendingRequest
        {
            get
            {
                lock (_lock)
                {
                    if (!_hasPendingRequest) return null;
                    return (_pendingRequestId, _pendingRequestPrompt);
                }
            }
        }

        // --- mode/goal/error setters --------------------------------------

        // Returns true when the supplied mode is valid (and applied).
        public bool SetMode(string mode)
        {
            if (mode != ModeAutonomous && mode != ModeRequest) return false;
            bool changed;
            lock (_lock)
            {
                changed = _mode != mode;
                _mode = mode;
            }
            if (changed) RaiseChanged();
            return true;
        }

        public void SetGoal(string goal)
        {
            var v = goal ?? "";
            bool changed;
            lock (_lock)
            {
                changed = _goal != v;
                _goal = v;
            }
            if (changed) RaiseChanged();
        }

        public void SetLastError(string error)
        {
            bool changed;
            lock (_lock)
            {
                changed = _lastError != error;
                _lastError = error;
            }
            if (changed) RaiseChanged();
        }

        // --- ready gate ---------------------------------------------------

        public void SetReady(bool ready)
        {
            bool changed;
            lock (_lock)
            {
                changed = _ready != ready;
                _ready = ready;
            }
            if (changed) RaiseChanged();
        }

        // --- pending request slot ----------------------------------------

        // Records a new prompt in the single pending slot. Overwrites any
        // existing pending request — the mod is intentionally single-slot;
        // queueing is the connector's job.
        public int EnqueueRequest(string prompt)
        {
            int id;
            lock (_lock)
            {
                _pendingRequestId = ++_nextRequestId;
                _pendingRequestPrompt = prompt ?? "";
                _hasPendingRequest = true;
                id = _pendingRequestId;
            }
            RaiseChanged();
            return id;
        }

        // Sets the pending slot to a specific (id, prompt). Used by inbound
        // WS messages where the agent itself proposes a request id (e.g.
        // tests, scripted prompts). For monotonic auto-allocation use
        // EnqueueRequest instead. Returns the supplied id for symmetry.
        public int SetPendingRequest(int id, string prompt)
        {
            lock (_lock)
            {
                if (id > _nextRequestId) _nextRequestId = id;
                _pendingRequestId = id;
                _pendingRequestPrompt = prompt ?? "";
                _hasPendingRequest = true;
            }
            RaiseChanged();
            return id;
        }

        // Returns true if the pending slot was cleared by this ack.
        public bool ClearPendingIfAcked(long ackedRequestId)
        {
            bool cleared;
            lock (_lock)
            {
                if (ackedRequestId > _lastAckedRequestId)
                    _lastAckedRequestId = ackedRequestId;
                if (_hasPendingRequest && ackedRequestId >= _pendingRequestId)
                {
                    _hasPendingRequest = false;
                    _pendingRequestPrompt = null;
                    cleared = true;
                }
                else cleared = false;
            }
            if (cleared) RaiseChanged();
            return cleared;
        }

        // Returns true if the pending slot was cleared by this heartbeat
        // (acked_request_id caught up to the pending request id). Side effects:
        //   - refreshes _lastHeartbeatUtc so the watchdog stays armed
        //   - records the connector-reported status string
        //   - bumps _lastAckedRequestId monotonically
        public bool Heartbeat(string agentStatus, long ackedRequestId, DateTime nowUtc)
        {
            bool cleared;
            bool snapshotChanged;
            lock (_lock)
            {
                _lastHeartbeatUtc = nowUtc;
                bool statusChanged = false;
                if (!string.IsNullOrEmpty(agentStatus) && _agentStatus != agentStatus)
                {
                    _agentStatus = agentStatus;
                    statusChanged = true;
                }
                if (ackedRequestId > _lastAckedRequestId)
                    _lastAckedRequestId = ackedRequestId;
                if (_hasPendingRequest && ackedRequestId >= _pendingRequestId)
                {
                    _hasPendingRequest = false;
                    _pendingRequestPrompt = null;
                    cleared = true;
                }
                else cleared = false;
                // _lastHeartbeatUtc and _lastAckedRequestId are not part of
                // ToStateResponseJson, so only agentStatus/pendingRequest
                // changes deserve a broadcast.
                snapshotChanged = statusChanged || cleared;
            }
            if (snapshotChanged) RaiseChanged();
            return cleared;
        }

        private void RaiseChanged()
        {
            // Snapshot the JSON outside the lock so concurrent readers can
            // proceed while subscribers fan-out the broadcast. Skip the
            // serialization entirely when nobody is listening (the common
            // case before any WS client connects).
            var handler = Changed;
            if (handler == null) return;
            string payload = ToStateResponseJson();
            try { handler(payload); } catch { /* swallow: WS push errors are non-fatal */ }
        }

        // --- persistence (state.json) ------------------------------------

        // Serialize only persisted fields. Ephemerals are intentionally
        // omitted: every game session starts with ready=false and an empty
        // pending slot.
        public string ToJson()
        {
            JObject obj;
            lock (_lock)
            {
                obj = new JObject
                {
                    ["mode"] = _mode,
                    ["goal"] = _goal,
                    ["lastError"] = _lastError,
                };
            }
            return obj.ToString(Formatting.Indented);
        }

        // Returns true if any persisted value was actually applied.
        public bool LoadJson(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return false;
            JObject parsed;
            try { parsed = JObject.Parse(json); }
            catch (JsonException) { return false; }
            lock (_lock)
            {
                var mode = parsed.Value<string>("mode");
                if (mode == ModeAutonomous || mode == ModeRequest)
                    _mode = mode;
                if (parsed["goal"] != null)
                    _goal = parsed.Value<string>("goal") ?? "";
                if (parsed["lastError"] != null)
                    _lastError = parsed.Value<string>("lastError");
                ResetEphemeralLocked();
            }
            return true;
        }

        // Resets ephemerals back to startup defaults. Public for the service
        // to invoke after a manual reload (e.g. the player loaded a different
        // save without restarting the game).
        public void ResetEphemerals()
        {
            lock (_lock) ResetEphemeralLocked();
        }

        private void ResetEphemeralLocked()
        {
            _ready = false;
            _pendingRequestId = 0;
            _pendingRequestPrompt = null;
            _hasPendingRequest = false;
            _lastAckedRequestId = 0;
            _agentStatus = "idle";
            _lastHeartbeatUtc = DateTime.MinValue;
            _nextRequestId = 0;
        }

        // Render the public /api/agent/state response as JSON. Single
        // serialization helper so the HTTP layer and tests can verify the
        // exact wire shape.
        public string ToStateResponseJson()
        {
            JObject obj;
            lock (_lock)
            {
                obj = new JObject
                {
                    ["mode"] = _mode,
                    ["goal"] = _goal,
                    ["ready"] = _ready,
                    ["agentStatus"] = _agentStatus,
                    ["lastError"] = _lastError,
                };
                if (_hasPendingRequest)
                {
                    obj["pendingRequest"] = new JObject
                    {
                        ["id"] = _pendingRequestId,
                        ["prompt"] = _pendingRequestPrompt ?? "",
                    };
                }
                else
                {
                    obj["pendingRequest"] = null;
                }
            }
            return obj.ToString(Formatting.None);
        }

        // --- gate carve-out ----------------------------------------------

        // Whitelist for the ready-gate middleware. Routes that match (case
        // insensitive, trailing slash trimmed) are served even while
        // ready=false. Everything else /api/* returns 409 game_not_ready.
        //
        // /api/tbot/* was deleted in the WS rework; the connector now talks
        // to the mod over the WS upgrade URL on `wsPort` instead.
        public static bool IsGateExempt(string path)
        {
            if (string.IsNullOrEmpty(path)) return false;
            if (path == "/api/ping") return true;
            if (path == "/api/ready") return true;
            if (path.StartsWith("/api/agent/", StringComparison.Ordinal)) return true;
            if (path == "/api/agent") return true;
            return false;
        }

        // Canonical 409 body. Kept on this class so the HTTP layer and tests
        // share a single source of truth.
        public const string GameNotReadyJson =
            "{\"error\":\"game_not_ready\",\"hint\":\"player must press Launch in the Timberbot widget\"}";
    }
}
