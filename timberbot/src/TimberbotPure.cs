using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

namespace Timberbot
{
    // Pure static helpers extracted from Unity-dependent classes for testability.
    // Original call sites delegate here via one-liners.
    public static class TimberbotPure
    {
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

        // --- security helpers ---

        private static readonly HashSet<string> BuiltinAllowedBinaries = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "claude", "codex"
        };

        public static bool IsAllowedBinary(string binary, HashSet<string> extraAllowed)
        {
            if (string.IsNullOrWhiteSpace(binary))
                return false;

            string name;
            try
            {
                name = Path.GetFileNameWithoutExtension(binary.Trim());
            }
            catch
            {
                return false;
            }

            if (string.IsNullOrEmpty(name))
                return false;

            if (BuiltinAllowedBinaries.Contains(name))
                return true;

            return extraAllowed != null && extraAllowed.Contains(name);
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
