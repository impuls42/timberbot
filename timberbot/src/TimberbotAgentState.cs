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
//     tbotWebhookUrl        : connector push URL; cleared if heartbeat lapses
//     lastAckedRequestId    : monotonic ack counter for the pending slot
//     agentStatus           : opaque connector-reported status string
//     lastHeartbeatUtc      : sliding window for the 6-second connector timeout
//
// All public mutators lock a single object so handlers running on the HTTP
// listener thread (e.g. /api/agent/state) and the Unity main thread (write-job
// drains) see a consistent snapshot. The container itself has no Unity or
// Timberborn dependency so it can be linked straight into the xUnit test
// assembly.
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

        // 6 seconds matches the connector heartbeat cadence in the design doc.
        // Anything older means the connector has died and its push URL must be
        // cleared so a stale localhost listener doesn't keep receiving events.
        public static readonly TimeSpan HeartbeatTimeout = TimeSpan.FromSeconds(6);

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
        private string _tbotWebhookUrl;
        private long _lastAckedRequestId;
        private string _agentStatus = "idle";
        private DateTime _lastHeartbeatUtc = DateTime.MinValue;
        private int _nextRequestId;

        public string Mode { get { lock (_lock) return _mode; } }
        public string Goal { get { lock (_lock) return _goal; } }
        public string LastError { get { lock (_lock) return _lastError; } }
        public bool Ready { get { lock (_lock) return _ready; } }
        public string AgentStatus { get { lock (_lock) return _agentStatus; } }
        public string TbotWebhookUrl { get { lock (_lock) return _tbotWebhookUrl; } }
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
            lock (_lock) _mode = mode;
            return true;
        }

        public void SetGoal(string goal)
        {
            lock (_lock) _goal = goal ?? "";
        }

        public void SetLastError(string error)
        {
            lock (_lock) _lastError = error;
        }

        // --- ready gate ---------------------------------------------------

        public void SetReady(bool ready)
        {
            lock (_lock) _ready = ready;
        }

        // --- pending request slot ----------------------------------------

        // Records a new prompt in the single pending slot. Overwrites any
        // existing pending request — the mod is intentionally single-slot;
        // queueing is the connector's job.
        public int EnqueueRequest(string prompt)
        {
            lock (_lock)
            {
                _pendingRequestId = ++_nextRequestId;
                _pendingRequestPrompt = prompt ?? "";
                _hasPendingRequest = true;
                return _pendingRequestId;
            }
        }

        // --- connector registration --------------------------------------

        public void RegisterTbotWebhook(string url, DateTime nowUtc)
        {
            lock (_lock)
            {
                _tbotWebhookUrl = url;
                _lastHeartbeatUtc = nowUtc;
            }
        }

        // Returns true if the pending slot was cleared by this heartbeat
        // (acked_request_id caught up to the pending request id). Side effects:
        //   - refreshes _lastHeartbeatUtc so the watchdog stays armed
        //   - records the connector-reported status string
        //   - bumps _lastAckedRequestId monotonically
        public bool Heartbeat(string agentStatus, long ackedRequestId, DateTime nowUtc)
        {
            lock (_lock)
            {
                _lastHeartbeatUtc = nowUtc;
                if (!string.IsNullOrEmpty(agentStatus))
                    _agentStatus = agentStatus;
                if (ackedRequestId > _lastAckedRequestId)
                    _lastAckedRequestId = ackedRequestId;
                if (_hasPendingRequest && ackedRequestId >= _pendingRequestId)
                {
                    _hasPendingRequest = false;
                    _pendingRequestPrompt = null;
                    return true;
                }
                return false;
            }
        }

        // Watchdog: if no heartbeat in HeartbeatTimeout, clear the push URL.
        // Idempotent — safe to call from any thread on a fixed schedule.
        // Returns true if it cleared a previously-set URL on this call.
        public bool ExpireWebhookIfStale(DateTime nowUtc)
        {
            lock (_lock)
            {
                if (string.IsNullOrEmpty(_tbotWebhookUrl)) return false;
                if (nowUtc - _lastHeartbeatUtc <= HeartbeatTimeout) return false;
                _tbotWebhookUrl = null;
                return true;
            }
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
            _tbotWebhookUrl = null;
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
        public static bool IsGateExempt(string path)
        {
            if (string.IsNullOrEmpty(path)) return false;
            if (path == "/api/ping") return true;
            if (path == "/api/ready") return true;
            if (path.StartsWith("/api/agent/", StringComparison.Ordinal)) return true;
            if (path == "/api/agent") return true;
            if (path.StartsWith("/api/tbot/", StringComparison.Ordinal)) return true;
            if (path == "/api/tbot") return true;
            return false;
        }

        // Canonical 409 body. Kept on this class so the HTTP layer and tests
        // share a single source of truth.
        public const string GameNotReadyJson =
            "{\"error\":\"game_not_ready\",\"hint\":\"player must press Launch in the Timberbot widget\"}";
    }
}
