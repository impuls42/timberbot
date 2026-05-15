using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using Newtonsoft.Json.Linq;

namespace Timberbot
{
    // Pure static helpers extracted from Unity-dependent classes for testability.
    // Original call sites delegate here via one-liners.
    public static class TimberbotPure
    {
        // Major version of the HTTP contract authored at /openapi.yaml.
        // Surfaced via /api/ping for client-side version checks. Bump when a
        // breaking change ships. The Python side has the same constant in
        // python/src/timberbot/__about__.py - keep them in lockstep.
        public const string OPENAPI_VERSION = "1.0.0";

        // Settings keys retired in PR 4. The mod still tolerates them on disk
        // for one release but never reads their values; DetectDeprecatedSettings
        // returns the subset present so the service can log a one-line warning.
        // Keep in lockstep with timberbot.settings.DEPRECATED_KEYS in Python.
        public static readonly string[] DEPRECATED_SETTINGS_KEYS = {
            "terminal",
            "pythonCommand",
            "agentModel",
            "agentEffort",
            "agentCommandTemplate",
            "agentAllowlistEnabled",
            "agentAllowedBinaries",
        };

        // Returns the deprecated keys present in `settings`. Detection-only —
        // does not mutate `settings`, so the values remain on disk for one
        // release before being stripped.
        public static List<string> DetectDeprecatedSettings(JObject settings)
        {
            var found = new List<string>();
            if (settings == null) return found;
            foreach (var key in DEPRECATED_SETTINGS_KEYS)
            {
                if (settings[key] != null)
                    found.Add(key);
            }
            return found;
        }

        // --- bearer-token auth helpers ---

        // True if the listen address is a loopback alias and therefore safe to
        // bind without an authToken. Anything else (a wildcard, a LAN IP, a
        // hostname) reaches outside the local box, so RequiresAuthToken kicks in.
        public static bool IsLoopbackAddress(string listenAddress)
        {
            if (string.IsNullOrWhiteSpace(listenAddress)) return true;
            var addr = listenAddress.Trim().ToLowerInvariant();
            return addr == "localhost" || addr == "127.0.0.1" || addr == "::1";
        }

        // Refuse-to-start check: a non-loopback bind without an authToken would
        // expose every /api/* endpoint to anyone who can reach the port. Empty
        // / whitespace tokens count as unset.
        public static bool RequiresAuthToken(string listenAddress, string authToken)
        {
            if (IsLoopbackAddress(listenAddress)) return false;
            return string.IsNullOrWhiteSpace(authToken);
        }

        // Normalize a configured auth token: null-coalesce + trim surrounding
        // whitespace so the server-side value matches the trimming that
        // ExtractBearerToken already applies to the client-presented value.
        // Without this, `"authToken": " s3cret "` in settings.json would never
        // match a client sending `Authorization: Bearer s3cret` because the
        // length check in BearerTokenMatches fails before any comparison.
        public static string NormalizeAuthToken(string authToken)
        {
            return (authToken ?? "").Trim();
        }

        // Extract the token from a `Bearer <token>` Authorization header
        // value, or null if the scheme is missing / wrong / empty. Scheme
        // match is case-insensitive per RFC 7235.
        public static string ExtractBearerToken(string authHeader)
        {
            if (string.IsNullOrWhiteSpace(authHeader)) return null;
            const string prefix = "Bearer ";
            if (authHeader.Length <= prefix.Length) return null;
            if (!authHeader.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return null;
            var token = authHeader.Substring(prefix.Length).Trim();
            return token.Length == 0 ? null : token;
        }

        // Constant-time comparison of an expected secret against a presented
        // bearer token, via CryptographicOperations.FixedTimeEquals — avoids
        // leaking the token through response-time side-channels.
        public static bool BearerTokenMatches(string expected, string presented)
        {
            if (string.IsNullOrEmpty(expected) || string.IsNullOrEmpty(presented))
                return false;
            var expectedBytes = Encoding.UTF8.GetBytes(expected);
            var presentedBytes = Encoding.UTF8.GetBytes(presented);
            // FixedTimeEquals requires equal-length inputs; the length
            // mismatch itself is a side-channel, but the server-side token
            // is fixed-length so its length is already public information.
            if (expectedBytes.Length != presentedBytes.Length) return false;
            return System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(expectedBytes, presentedBytes);
        }

        // --- from TimberbotAgent ---

        public static string JsonEscape(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            if (s.Length > 2000) s = s.Substring(0, 2000) + "...(truncated)";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"")
                    .Replace("\n", "\\n").Replace("\r", "\\r").Replace("\t", "\\t");
        }

        public static bool IsCodexBinary(string binary)
        {
            if (string.IsNullOrWhiteSpace(binary))
                return false;

            try
            {
                return string.Equals(Path.GetFileNameWithoutExtension(binary.Trim()), "codex", StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return false;
            }
        }

        public static string QuoteArg(string value)
        {
            if (value == null)
                value = "";
            return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
        }

        public static string ShellQuoteArg(string value)
        {
            if (value == null)
                value = "";
            return "'" + value.Replace("'", "'\"'\"'") + "'";
        }

        // BuildTbotAgentRunArgv / FormatArgvForDisplay used to live here. The
        // mod no longer spawns `tbot agent run` from C#: the new architecture
        // (see #12) inverts the relationship — the Python `tbot watch`
        // connector polls the mod, and the player drives the agent via the
        // in-game widget's Launch button (POST /api/ready). With the spawn
        // path gone, the argv builder and its display formatter went with it.

        // Connection-state pill rendered by TimberbotPanel. Extracted here so
        // the classification logic can be unit-tested without dragging Unity
        // (Color, VisualElement) into the test project.
        public enum ConnectionPillState
        {
            Disconnected,
            NotReady,
            Idle,
            Running,
            Error,
        }

        // Maps the /api/agent/state poll outcome to the pill state + whether
        // the gate is open. `gateOn=true` means the Stop button is the active
        // half of the Launch/Stop pair, including when the state is Error so
        // the player can always Stop out of a stuck cycle.
        public static (ConnectionPillState pill, bool gateOn) ClassifyConnection(
            bool pollOk, JObject state)
        {
            if (!pollOk || state == null)
                return (ConnectionPillState.Disconnected, false);

            var ready = state.Value<bool?>("ready") ?? false;
            var lastError = state.Value<string>("lastError");
            if (!string.IsNullOrEmpty(lastError))
                return (ConnectionPillState.Error, ready);

            if (!ready)
                return (ConnectionPillState.NotReady, false);

            var pending = state["pendingRequest"];
            var hasPending = pending != null && pending.Type != JTokenType.Null;
            var agentStatus = ExtractAgentStatusString(state["agentStatus"]);
            var running = hasPending || IsAgentStatusBusy(agentStatus);
            return running
                ? (ConnectionPillState.Running, true)
                : (ConnectionPillState.Idle, true);
        }

        // /api/agent/state ships agentStatus as a free-form object (mirrors the
        // openapi.yaml schema). We treat the connector's "status" field as the
        // authoritative string when present, else fall back to a top-level
        // string. Returns lowercased value or "" when absent.
        public static string ExtractAgentStatusString(JToken agentStatus)
        {
            if (agentStatus == null || agentStatus.Type == JTokenType.Null) return "";
            if (agentStatus.Type == JTokenType.String) return ((string)agentStatus ?? "").ToLowerInvariant();
            if (agentStatus is JObject obj)
            {
                var s = obj.Value<string>("status");
                if (!string.IsNullOrEmpty(s)) return s.ToLowerInvariant();
            }
            return "";
        }

        // Treat anything that isn't an obviously-idle status name as busy.
        // Keeps the widget honest if the connector ships a new status verb.
        public static bool IsAgentStatusBusy(string lowercased)
        {
            if (string.IsNullOrEmpty(lowercased)) return false;
            switch (lowercased)
            {
                case "idle":
                case "done":
                case "ready":
                case "disconnected":
                    return false;
                default:
                    return true;
            }
        }

        // Normalize the mode dropdown's text value to the openapi.yaml enum.
        // Anything not exactly "request" (case-insensitive, trimmed) falls
        // back to "autonomous".
        public static string NormalizeMode(string raw)
        {
            var v = (raw ?? "").Trim().ToLowerInvariant();
            return v == "request" ? "request" : "autonomous";
        }

        public static bool ValidateWebhookUrlFormat(string url, out string error)
        {
            error = null;

            if (string.IsNullOrWhiteSpace(url))
            {
                error = "invalid_webhook_url: url is empty";
                return false;
            }

            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri))
            {
                error = "invalid_webhook_url: malformed url";
                return false;
            }

            var scheme = uri.Scheme.ToLowerInvariant();
            if (scheme != "http" && scheme != "https")
            {
                error = "invalid_webhook_url: scheme must be http or https, got " + scheme;
                return false;
            }

            if (string.IsNullOrWhiteSpace(uri.Host))
            {
                error = "invalid_webhook_url: no host in url";
                return false;
            }

            return true;
        }

        // --- from TimberbotPlacement ---

        public static int ParseOrientation(string orient)
        {
            if (string.IsNullOrEmpty(orient)) return 0;
            var lower = orient.Trim().ToLowerInvariant();
            switch (lower)
            {
                case "south": return 0;
                case "west": return 1;
                case "north": return 2;
                case "east": return 3;
                default: return -1;
            }
        }

        // --- from TimberbotEntityRegistry ---

        public static string CanonicalName(string name)
        {
            return name.Replace("(Clone)", "").Trim();
        }

        public static string CleanName(string name, string factionSuffix)
        {
            var clean = CanonicalName(name);
            if (factionSuffix != null && factionSuffix.Length > 0)
                clean = clean.Replace(factionSuffix, "");
            return clean.Trim();
        }

        // --- from TimberbotDebug ---

        public static bool TryGetNumeric(object value, out double numeric)
        {
            numeric = 0;
            if (value == null) return false;
            try
            {
                if (value is bool b) { numeric = b ? 1 : 0; return true; }
                if (value is IConvertible c) { numeric = Convert.ToDouble(c, CultureInfo.InvariantCulture); return true; }
            }
            catch { }
            return false;
        }

        public static bool ValuesEqual(object left, object right)
        {
            if (left == null || right == null) return left == right;
            if (TryGetNumeric(left, out var leftNum) && TryGetNumeric(right, out var rightNum))
                return Math.Abs(leftNum - rightNum) < 0.0001;
            return Equals(left, right);
        }

        public static int CompareValues(object left, object right, out bool comparable)
        {
            comparable = false;
            if (TryGetNumeric(left, out var leftNum) && TryGetNumeric(right, out var rightNum))
            {
                comparable = true;
                return leftNum.CompareTo(rightNum);
            }
            if (left is string ls && right is string rs)
            {
                comparable = true;
                return string.Compare(ls, rs, StringComparison.Ordinal);
            }
            return 0;
        }

        public static bool EvaluateAssertion(object left, string op, object right, out string detail)
        {
            detail = null;
            switch (op)
            {
                case "eq": return ValuesEqual(left, right);
                case "neq": return !ValuesEqual(left, right);
                case "null": return left == null;
                case "notnull": return left != null;
                case "gt":
                case "gte":
                case "lt":
                case "lte":
                    var cmp = CompareValues(left, right, out var comparable);
                    if (!comparable) { detail = "values not comparable"; return false; }
                    if (op == "gt") return cmp > 0;
                    if (op == "gte") return cmp >= 0;
                    if (op == "lt") return cmp < 0;
                    return cmp <= 0;
                default:
                    detail = $"unknown op '{op}'";
                    return false;
            }
        }

        // --- from TimberbotPanel ---

        public static string NormalizeValue(string value, string fallback)
        {
            return string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
        }

        public static string NormalizeBoolString(string value, bool fallback)
        {
            var normalized = NormalizeValue(value, fallback ? "true" : "false").ToLowerInvariant();
            return normalized == "false" ? "false" : "true";
        }

        public static string NormalizeIntString(string value, int fallback, int minValue)
        {
            if (int.TryParse(NormalizeValue(value, fallback.ToString()), out var parsed) && parsed >= minValue)
                return parsed.ToString();

            return fallback.ToString();
        }

        public static string NormalizeDoubleString(string value, double fallback, double minValue)
        {
            if (double.TryParse(NormalizeValue(value, fallback.ToString(CultureInfo.InvariantCulture)), NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) && parsed >= minValue)
                return parsed.ToString(CultureInfo.InvariantCulture);

            return fallback.ToString(CultureInfo.InvariantCulture);
        }

        // --- from TimberbotReadV2 ---

        public static bool PassesFilter(string entityName, int entityX, int entityY,
            string filterName, int filterX, int filterY, int filterRadius)
        {
            if (filterName != null && entityName.IndexOf(filterName, StringComparison.OrdinalIgnoreCase) < 0)
                return false;
            if (filterRadius > 0 && (Math.Abs(entityX - filterX) + Math.Abs(entityY - filterY)) > filterRadius)
                return false;
            return true;
        }

        public static string ToToonDict(Dictionary<string, int> dict)
        {
            if (dict == null || dict.Count == 0) return "";
            var sb = new StringBuilder(256);
            foreach (var kvp in dict)
            {
                if (sb.Length > 0) sb.Append('/');
                sb.Append(kvp.Key).Append(':').Append(kvp.Value);
            }
            return sb.ToString();
        }

        public static string GetBeaverTier(float wellbeing, bool isBot)
        {
            if (isBot) return "operational";
            if (wellbeing >= 16) return "ecstatic";
            if (wellbeing >= 12) return "happy";
            if (wellbeing >= 8) return "okay";
            if (wellbeing >= 4) return "unhappy";
            return "miserable";
        }

        // --- from TimberbotWrite ---

        public static string DeterminePriorityToSet(bool finished, bool hasWorkplacePrio, bool hasBuilderPrio)
        {
            // Smart auto-detect: prefer workplace if constructed, otherwise construction
            if (finished && hasWorkplacePrio) return "workplace";
            if (hasBuilderPrio) return "construction";
            if (hasWorkplacePrio) return "workplace";
            return null;
        }

        public static string DetermineAutomationType(
            bool hasRelay, bool hasMemory, bool hasLever, bool hasChronometer,
            bool hasDepthSensor, bool hasContaminationSensor, bool hasFlowSensor,
            bool hasResourceCounter, bool hasPopulationCounter, bool hasPowerMeter,
            bool hasAutomatable)
        {
            if (hasRelay) return "Relay";
            if (hasMemory) return "Memory";
            if (hasLever) return "Lever";
            if (hasChronometer) return "Chronometer";
            if (hasDepthSensor) return "DepthSensor";
            if (hasContaminationSensor) return "ContaminationSensor";
            if (hasFlowSensor) return "FlowSensor";
            if (hasResourceCounter) return "ResourceCounter";
            if (hasPopulationCounter) return "PopulationCounter";
            if (hasPowerMeter) return "PowerMeter";
            if (hasAutomatable) return "Automatable";
            return "";
        }
    }

    public sealed class PureCollectionQuery
    {
        public string Format;
        public int? SingleId;
        public int Limit;
        public int Offset;
        public string FilterName;
        public int FilterX;
        public int FilterY;
        public int FilterRadius;
        public bool HasFilter;
        public bool Paginated;
        public bool NeedsFullDetail;

        public static PureCollectionQuery Parse(string format, string detail, int id, int limit, int offset, string filterName, int filterX, int filterY, int filterRadius)
        {
            int? singleId = id != 0 ? id : (int?)null;
            if (!singleId.HasValue && !string.IsNullOrEmpty(detail) && detail.StartsWith("id:", StringComparison.Ordinal))
            {
                if (int.TryParse(detail.Substring(3), out int parsed))
                    singleId = parsed;
            }
            return new PureCollectionQuery
            {
                Format = format ?? "toon",
                SingleId = singleId,
                Limit = limit,
                Offset = offset,
                FilterName = filterName,
                FilterX = filterX,
                FilterY = filterY,
                FilterRadius = filterRadius,
                HasFilter = filterName != null || filterRadius > 0,
                Paginated = limit > 0 && !singleId.HasValue,
                NeedsFullDetail = detail == "full" || singleId.HasValue
            };
        }
    }
}
