using Xunit;
using Timberbot;
using Newtonsoft.Json.Linq;

namespace Timberbot.Tests
{
    public class DetectDeprecatedSettingsTests
    {
        [Fact]
        public void NullInput_ReturnsEmptyList()
        {
            var got = TimberbotPure.DetectDeprecatedSettings(null);
            Assert.NotNull(got);
            Assert.Empty(got);
        }

        [Fact]
        public void CleanInput_ReturnsEmptyList()
        {
            var json = JObject.Parse("{\"httpPort\": 8085, \"webhooksEnabled\": true}");
            Assert.Empty(TimberbotPure.DetectDeprecatedSettings(json));
        }

        [Fact]
        public void DetectsEveryDeprecatedKey()
        {
            var json = JObject.Parse("{\"terminal\":\"wt\",\"pythonCommand\":\"py3\"," +
                                    "\"agentModel\":\"sonnet\",\"agentEffort\":\"high\",\"agentCommandTemplate\":\"x\"," +
                                    "\"agentAllowlistEnabled\":false,\"agentAllowedBinaries\":[\"opencode\"]," +
                                    "\"httpPort\":8085}");
            var got = TimberbotPure.DetectDeprecatedSettings(json);
            Assert.Equal(TimberbotPure.DEPRECATED_SETTINGS_KEYS.Length, got.Count);
            foreach (var key in TimberbotPure.DEPRECATED_SETTINGS_KEYS)
                Assert.Contains(key, got);
        }

        [Fact]
        public void DoesNotDetectAgentBinary()
        {
            // agentBinary is the storage key for the still-active Backend
            // dropdown — listing it as deprecated would cause a warning-loop
            // each time the panel saved the field back.
            var json = JObject.Parse("{\"agentBinary\":\"claude\"}");
            Assert.Empty(TimberbotPure.DetectDeprecatedSettings(json));
        }

        [Fact]
        public void DoesNotMutateInput()
        {
            var json = JObject.Parse("{\"terminal\":\"wt\"}");
            TimberbotPure.DetectDeprecatedSettings(json);
            // Detection is a one-release-grace policy: keys stay on disk so old
            // tooling can still read them. The strip happens in a future release.
            Assert.Equal("wt", json.Value<string>("terminal"));
        }
    }


    public class JsonEscapeTests
    {
        [Fact]
        public void Null_ReturnsEmpty() => Assert.Equal("", TimberbotPure.JsonEscape(null));

        [Fact]
        public void Empty_ReturnsEmpty() => Assert.Equal("", TimberbotPure.JsonEscape(""));

        [Fact]
        public void Normal_Unchanged() => Assert.Equal("hello", TimberbotPure.JsonEscape("hello"));

        [Fact]
        public void Backslash_Escaped() => Assert.Equal("a\\\\b", TimberbotPure.JsonEscape("a\\b"));

        [Fact]
        public void Quote_Escaped() => Assert.Equal("say \\\"hi\\\"", TimberbotPure.JsonEscape("say \"hi\""));

        [Fact]
        public void Newline_Escaped() => Assert.Equal("a\\nb", TimberbotPure.JsonEscape("a\nb"));

        [Fact]
        public void Tab_Escaped() => Assert.Equal("a\\tb", TimberbotPure.JsonEscape("a\tb"));

        [Fact]
        public void CarriageReturn_Escaped() => Assert.Equal("a\\rb", TimberbotPure.JsonEscape("a\rb"));

        [Fact]
        public void LongString_Truncated()
        {
            var input = new string('x', 2500);
            var result = TimberbotPure.JsonEscape(input);
            Assert.EndsWith("...(truncated)", result);
            Assert.True(result.Length < 2500);
        }

        [Fact]
        public void ExactlyAtLimit_NotTruncated()
        {
            var input = new string('x', 2000);
            var result = TimberbotPure.JsonEscape(input);
            Assert.Equal(2000, result.Length);
            Assert.DoesNotContain("truncated", result);
        }
    }

    public class IsCodexBinaryTests
    {
        [Theory]
        [InlineData("codex", true)]
        [InlineData("Codex", true)]
        [InlineData("CODEX", true)]
        [InlineData("codex.exe", true)]
        [InlineData("claude", false)]
        [InlineData("", false)]
        [InlineData(null, false)]
        [InlineData("  codex  ", true)]
        public void DetectsCodex(string input, bool expected) =>
            Assert.Equal(expected, TimberbotPure.IsCodexBinary(input));
    }

    public class QuoteArgTests
    {
        [Fact]
        public void Null_QuotedEmpty() => Assert.Equal("\"\"", TimberbotPure.QuoteArg(null));

        [Fact]
        public void Empty_QuotedEmpty() => Assert.Equal("\"\"", TimberbotPure.QuoteArg(""));

        [Fact]
        public void Normal_Quoted() => Assert.Equal("\"hello\"", TimberbotPure.QuoteArg("hello"));

        [Fact]
        public void Backslash_Escaped() => Assert.Equal("\"a\\\\b\"", TimberbotPure.QuoteArg("a\\b"));

        [Fact]
        public void InnerQuote_Escaped() => Assert.Equal("\"say \\\"hi\\\"\"", TimberbotPure.QuoteArg("say \"hi\""));
    }

    public class ShellQuoteArgTests
    {
        [Fact]
        public void Null_QuotedEmpty() => Assert.Equal("''", TimberbotPure.ShellQuoteArg(null));

        [Fact]
        public void Empty_QuotedEmpty() => Assert.Equal("''", TimberbotPure.ShellQuoteArg(""));

        [Fact]
        public void Normal_Quoted() => Assert.Equal("'hello'", TimberbotPure.ShellQuoteArg("hello"));

        [Fact]
        public void SingleQuote_Escaped() =>
            Assert.Equal("'it'\"'\"'s'", TimberbotPure.ShellQuoteArg("it's"));
    }

    public class ParseOrientationTests
    {
        [Theory]
        [InlineData("south", 0)]
        [InlineData("west", 1)]
        [InlineData("north", 2)]
        [InlineData("east", 3)]
        [InlineData("SOUTH", 0)]
        [InlineData("North", 2)]
        [InlineData(" north ", 2)]
        [InlineData("invalid", -1)]
        public void ParsesDirection(string input, int expected) =>
            Assert.Equal(expected, TimberbotPure.ParseOrientation(input));

        [Fact]
        public void Null_ReturnsSouth() => Assert.Equal(0, TimberbotPure.ParseOrientation(null));

        [Fact]
        public void Empty_ReturnsSouth() => Assert.Equal(0, TimberbotPure.ParseOrientation(""));
    }

    public class CanonicalNameTests
    {
        [Fact]
        public void RemovesCloneSuffix() => Assert.Equal("Path", TimberbotPure.CanonicalName("Path(Clone)"));

        [Fact]
        public void NoSuffix_Unchanged() => Assert.Equal("Path", TimberbotPure.CanonicalName("Path"));

        [Fact]
        public void Trims_Whitespace() => Assert.Equal("Path", TimberbotPure.CanonicalName("  Path  "));

        [Fact]
        public void Empty_ReturnsEmpty() => Assert.Equal("", TimberbotPure.CanonicalName(""));
    }

    public class CleanNameTests
    {
        [Fact]
        public void RemovesFactionSuffix() =>
            Assert.Equal("Lumberjack", TimberbotPure.CleanName("Lumberjack.Folktails", ".Folktails"));

        [Fact]
        public void RemovesCloneAndFaction() =>
            Assert.Equal("Lumberjack", TimberbotPure.CleanName("Lumberjack.Folktails(Clone)", ".Folktails"));

        [Fact]
        public void NullSuffix_JustCleansClone() =>
            Assert.Equal("Path", TimberbotPure.CleanName("Path(Clone)", null));

        [Fact]
        public void EmptySuffix_JustCleansClone() =>
            Assert.Equal("Path", TimberbotPure.CleanName("Path(Clone)", ""));
    }

    public class ValuesEqualTests
    {
        [Fact]
        public void BothNull_Equal() => Assert.True(TimberbotPure.ValuesEqual(null, null));

        [Fact]
        public void OneNull_NotEqual() => Assert.False(TimberbotPure.ValuesEqual(null, 1));

        [Fact]
        public void SameInt_Equal() => Assert.True(TimberbotPure.ValuesEqual(1, 1));

        [Fact]
        public void DifferentInt_NotEqual() => Assert.False(TimberbotPure.ValuesEqual(1, 2));

        [Fact]
        public void IntFloat_CloseEnough() => Assert.True(TimberbotPure.ValuesEqual(1, 1.00005));

        [Fact]
        public void IntFloat_TooFar() => Assert.False(TimberbotPure.ValuesEqual(1, 1.001));

        [Fact]
        public void SameString_Equal() => Assert.True(TimberbotPure.ValuesEqual("a", "a"));

        [Fact]
        public void DifferentString_NotEqual() => Assert.False(TimberbotPure.ValuesEqual("a", "b"));
    }

    public class TryGetNumericTests
    {
        [Fact]
        public void Int_Converts()
        {
            Assert.True(TimberbotPure.TryGetNumeric(42, out var n));
            Assert.Equal(42.0, n);
        }

        [Fact]
        public void BoolTrue_IsOne()
        {
            Assert.True(TimberbotPure.TryGetNumeric(true, out var n));
            Assert.Equal(1.0, n);
        }

        [Fact]
        public void BoolFalse_IsZero()
        {
            Assert.True(TimberbotPure.TryGetNumeric(false, out var n));
            Assert.Equal(0.0, n);
        }

        [Fact]
        public void Null_ReturnsFalse() => Assert.False(TimberbotPure.TryGetNumeric(null, out _));

        [Fact]
        public void String_Number_Converts()
        {
            // string implements IConvertible
            Assert.True(TimberbotPure.TryGetNumeric("3.14", out var n));
            Assert.Equal(3.14, n, 2);
        }
    }

    public class CompareValuesTests
    {
        [Fact]
        public void Ints_Comparable()
        {
            var result = TimberbotPure.CompareValues(5, 3, out var comparable);
            Assert.True(comparable);
            Assert.True(result > 0);
        }

        [Fact]
        public void Strings_Comparable()
        {
            var result = TimberbotPure.CompareValues("a", "b", out var comparable);
            Assert.True(comparable);
            Assert.True(result < 0);
        }

        [Fact]
        public void Incomparable_ReturnsFalse()
        {
            TimberbotPure.CompareValues(new object(), new object(), out var comparable);
            Assert.False(comparable);
        }
    }

    public class EvaluateAssertionTests
    {
        [Fact]
        public void Eq_True() => Assert.True(TimberbotPure.EvaluateAssertion(1, "eq", 1, out _));

        [Fact]
        public void Eq_False() => Assert.False(TimberbotPure.EvaluateAssertion(1, "eq", 2, out _));

        [Fact]
        public void Neq_True() => Assert.True(TimberbotPure.EvaluateAssertion(1, "neq", 2, out _));

        [Fact]
        public void Null_True() => Assert.True(TimberbotPure.EvaluateAssertion(null, "null", null, out _));

        [Fact]
        public void Null_False() => Assert.False(TimberbotPure.EvaluateAssertion(1, "null", null, out _));

        [Fact]
        public void Notnull_True() => Assert.True(TimberbotPure.EvaluateAssertion(1, "notnull", null, out _));

        [Fact]
        public void Gt_True() => Assert.True(TimberbotPure.EvaluateAssertion(5, "gt", 3, out _));

        [Fact]
        public void Gt_False() => Assert.False(TimberbotPure.EvaluateAssertion(3, "gt", 5, out _));

        [Fact]
        public void Gte_Equal() => Assert.True(TimberbotPure.EvaluateAssertion(3, "gte", 3, out _));

        [Fact]
        public void Lt_True() => Assert.True(TimberbotPure.EvaluateAssertion(3, "lt", 5, out _));

        [Fact]
        public void Lte_Equal() => Assert.True(TimberbotPure.EvaluateAssertion(3, "lte", 3, out _));

        [Fact]
        public void UnknownOp_FalseWithDetail()
        {
            var result = TimberbotPure.EvaluateAssertion(1, "xyz", 2, out var detail);
            Assert.False(result);
            Assert.Contains("unknown op", detail);
        }

        [Fact]
        public void Gt_Incomparable_FalseWithDetail()
        {
            var result = TimberbotPure.EvaluateAssertion(new object(), "gt", new object(), out var detail);
            Assert.False(result);
            Assert.Equal("values not comparable", detail);
        }
    }

    public class NormalizeValueTests
    {
        [Fact]
        public void Normal_Trimmed() => Assert.Equal("hello", TimberbotPure.NormalizeValue("  hello  ", "def"));

        [Fact]
        public void Null_ReturnsFallback() => Assert.Equal("def", TimberbotPure.NormalizeValue(null, "def"));

        [Fact]
        public void Empty_ReturnsFallback() => Assert.Equal("def", TimberbotPure.NormalizeValue("", "def"));

        [Fact]
        public void Whitespace_ReturnsFallback() => Assert.Equal("def", TimberbotPure.NormalizeValue("   ", "def"));
    }

    public class NormalizeBoolStringTests
    {
        [Fact]
        public void True_ReturnsTrue() => Assert.Equal("true", TimberbotPure.NormalizeBoolString("true", false));

        [Fact]
        public void False_ReturnsFalse() => Assert.Equal("false", TimberbotPure.NormalizeBoolString("false", true));

        [Fact]
        public void Null_ReturnsFallback() => Assert.Equal("true", TimberbotPure.NormalizeBoolString(null, true));

        [Fact]
        public void Garbage_ReturnsTrue() =>
            Assert.Equal("true", TimberbotPure.NormalizeBoolString("garbage", false));
    }

    public class NormalizeIntStringTests
    {
        [Fact]
        public void Valid_ReturnsValue() => Assert.Equal("42", TimberbotPure.NormalizeIntString("42", 10, 0));

        [Fact]
        public void BelowMin_ReturnsFallback() => Assert.Equal("10", TimberbotPure.NormalizeIntString("-5", 10, 0));

        [Fact]
        public void AtMin_ReturnsValue() => Assert.Equal("0", TimberbotPure.NormalizeIntString("0", 10, 0));

        [Fact]
        public void Null_ReturnsFallback() => Assert.Equal("10", TimberbotPure.NormalizeIntString(null, 10, 0));

        [Fact]
        public void NotANumber_ReturnsFallback() => Assert.Equal("10", TimberbotPure.NormalizeIntString("abc", 10, 0));
    }

    public class ValidateWebhookUrlFormatTests
    {
        [Fact]
        public void HttpValid() => Assert.True(TimberbotPure.ValidateWebhookUrlFormat("http://example.com/hook", out _));

        [Fact]
        public void HttpsValid() => Assert.True(TimberbotPure.ValidateWebhookUrlFormat("https://example.com/hook", out _));

        [Fact]
        public void FtpRejected()
        {
            Assert.False(TimberbotPure.ValidateWebhookUrlFormat("ftp://example.com", out var err));
            Assert.Contains("scheme", err);
        }

        [Fact]
        public void FileRejected()
        {
            Assert.False(TimberbotPure.ValidateWebhookUrlFormat("file:///etc/passwd", out var err));
            Assert.Contains("scheme", err);
        }

        [Fact]
        public void EmptyRejected()
        {
            Assert.False(TimberbotPure.ValidateWebhookUrlFormat("", out var err));
            Assert.Contains("empty", err);
        }

        [Fact]
        public void NullRejected()
        {
            Assert.False(TimberbotPure.ValidateWebhookUrlFormat(null, out var err));
            Assert.Contains("empty", err);
        }

        [Fact]
        public void MalformedRejected()
        {
            Assert.False(TimberbotPure.ValidateWebhookUrlFormat("not a url", out var err));
            Assert.Contains("malformed", err);
        }
    }

    public class NormalizeDoubleStringTests
    {
        [Fact]
        public void Valid_ReturnsValue() => Assert.Equal("1.5", TimberbotPure.NormalizeDoubleString("1.5", 0.5, 0.0));

        [Fact]
        public void BelowMin_ReturnsFallback() =>
            Assert.Equal("0.5", TimberbotPure.NormalizeDoubleString("-1.0", 0.5, 0.0));

        [Fact]
        public void Null_ReturnsFallback() =>
            Assert.Equal("0.5", TimberbotPure.NormalizeDoubleString(null, 0.5, 0.0));

        [Fact]
        public void NotANumber_ReturnsFallback() =>
            Assert.Equal("0.5", TimberbotPure.NormalizeDoubleString("abc", 0.5, 0.0));
    }

    public class PassesFilterTests
    {
        [Theory]
        [InlineData("Lodge", 10, 10, null, 0, 0, 0, true)]
        [InlineData("Lodge", 10, 10, "Lodge", 0, 0, 0, true)]
        [InlineData("Lodge", 10, 10, "Farm", 0, 0, 0, false)]
        [InlineData("Lodge", 10, 10, "lodge", 0, 0, 0, true)]
        [InlineData("Lodge", 10, 10, null, 10, 10, 5, true)]
        [InlineData("Lodge", 20, 20, null, 10, 10, 5, false)]
        [InlineData("Lodge", 12, 13, null, 10, 10, 5, true)] // dist = 2 + 3 = 5
        [InlineData("Lodge", 12, 14, null, 10, 10, 5, false)] // dist = 2 + 4 = 6
        [InlineData("Lumberjack", 10, 10, "Lumber", 10, 10, 1, true)]
        public void FilteringLogic(string name, int x, int y, string fName, int fX, int fY, int fRadius, bool expected) =>
            Assert.Equal(expected, TimberbotPure.PassesFilter(name, x, y, fName, fX, fY, fRadius));
    }

    public class ToToonDictTests
    {
        [Fact]
        public void Empty_ReturnsEmpty() => Assert.Equal("", TimberbotPure.ToToonDict(new System.Collections.Generic.Dictionary<string, int>()));

        [Fact]
        public void Null_ReturnsEmpty() => Assert.Equal("", TimberbotPure.ToToonDict(null));

        [Fact]
        public void SingleItem() =>
            Assert.Equal("Log:10", TimberbotPure.ToToonDict(new System.Collections.Generic.Dictionary<string, int> { { "Log", 10 } }));

        [Fact]
        public void MultipleItems() =>
            Assert.Equal("Log:10/Plank:5", TimberbotPure.ToToonDict(new System.Collections.Generic.Dictionary<string, int> { { "Log", 10 }, { "Plank", 5 } }));
    }

    public class GetBeaverTierTests
    {
        [Theory]
        [InlineData(20f, false, "ecstatic")]
        [InlineData(16f, false, "ecstatic")]
        [InlineData(15.9f, false, "happy")]
        [InlineData(12f, false, "happy")]
        [InlineData(11.9f, false, "okay")]
        [InlineData(8f, false, "okay")]
        [InlineData(7.9f, false, "unhappy")]
        [InlineData(4f, false, "unhappy")]
        [InlineData(3.9f, false, "miserable")]
        [InlineData(0f, false, "miserable")]
        [InlineData(20f, true, "operational")]
        [InlineData(0f, true, "operational")]
        public void TierMapping(float wb, bool isBot, string expected) =>
            Assert.Equal(expected, TimberbotPure.GetBeaverTier(wb, isBot));
    }

    public class DeterminePriorityToSetTests
    {
        [Theory]
        [InlineData(true, true, true, "workplace")]
        [InlineData(true, true, false, "workplace")]
        [InlineData(true, false, true, "construction")]
        [InlineData(false, true, true, "construction")]
        [InlineData(false, false, true, "construction")]
        [InlineData(false, true, false, "workplace")]
        [InlineData(true, false, false, null)]
        public void PriorityAutoDetect(bool finished, bool hasWp, bool hasBuilder, string expected) =>
            Assert.Equal(expected, TimberbotPure.DeterminePriorityToSet(finished, hasWp, hasBuilder));
    }

    public class DetermineAutomationTypeTests
    {
        [Fact]
        public void DetectsRelay() =>
            Assert.Equal("Relay", TimberbotPure.DetermineAutomationType(true, false, false, false, false, false, false, false, false, false, true));

        [Fact]
        public void DetectsMemory() =>
            Assert.Equal("Memory", TimberbotPure.DetermineAutomationType(false, true, false, false, false, false, false, false, false, false, true));

        [Fact]
        public void DetectsAutomatable_Fallback() =>
            Assert.Equal("Automatable", TimberbotPure.DetermineAutomationType(false, false, false, false, false, false, false, false, false, false, true));

        [Fact]
        public void ReturnsEmpty_IfNone() =>
            Assert.Equal("", TimberbotPure.DetermineAutomationType(false, false, false, false, false, false, false, false, false, false, false));

        [Fact]
        public void Priority_RelayOverAutomatable() =>
            Assert.Equal("Relay", TimberbotPure.DetermineAutomationType(true, false, false, false, false, false, false, false, false, false, true));
    }

    public class PureCollectionQueryTests
    {
        [Fact]
        public void Parse_Basic()
        {
            var q = PureCollectionQuery.Parse("json", "full", 0, 10, 20, null, 0, 0, 0);
            Assert.Equal("json", q.Format);
            Assert.Null(q.SingleId);
            Assert.Equal(10, q.Limit);
            Assert.Equal(20, q.Offset);
            Assert.True(q.NeedsFullDetail);
            Assert.True(q.Paginated);
            Assert.False(q.HasFilter);
        }

        [Fact]
        public void Parse_SingleId_Param()
        {
            var q = PureCollectionQuery.Parse(null, null, 123, 0, 0, null, 0, 0, 0);
            Assert.Equal(123, q.SingleId);
            Assert.True(q.NeedsFullDetail);
            Assert.False(q.Paginated);
        }

        [Fact]
        public void Parse_SingleId_DetailPrefix()
        {
            var q = PureCollectionQuery.Parse(null, "id:456", 0, 0, 0, null, 0, 0, 0);
            Assert.Equal(456, q.SingleId);
            Assert.True(q.NeedsFullDetail);
        }

        [Fact]
        public void Parse_Filter()
        {
            var q = PureCollectionQuery.Parse(null, null, 0, 0, 0, "test", 1, 2, 3);
            Assert.True(q.HasFilter);
            Assert.Equal("test", q.FilterName);
            Assert.Equal(1, q.FilterX);
            Assert.Equal(2, q.FilterY);
            Assert.Equal(3, q.FilterRadius);
        }
    }

    public class NormalizeModeTests
    {
        [Theory]
        [InlineData("autonomous", "autonomous")]
        [InlineData("request", "request")]
        [InlineData("REQUEST", "request")]
        [InlineData("  request  ", "request")]
        [InlineData("Autonomous", "autonomous")]
        [InlineData("", "autonomous")]
        [InlineData(null, "autonomous")]
        [InlineData("garbage", "autonomous")]
        public void NormalizesMode(string input, string expected) =>
            Assert.Equal(expected, TimberbotPure.NormalizeMode(input));
    }

    public class IsAgentStatusBusyTests
    {
        [Theory]
        [InlineData("idle", false)]
        [InlineData("done", false)]
        [InlineData("ready", false)]
        [InlineData("disconnected", false)]
        [InlineData("", false)]
        [InlineData(null, false)]
        // Default-to-busy keeps the widget honest if the connector ships a
        // new status verb (e.g. "thinking", "writing", "tool_use").
        [InlineData("running", true)]
        [InlineData("thinking", true)]
        [InlineData("tool_use", true)]
        public void ClassifiesBusyState(string status, bool expected) =>
            Assert.Equal(expected, TimberbotPure.IsAgentStatusBusy(status));
    }

    public class ExtractAgentStatusStringTests
    {
        [Fact]
        public void Null_ReturnsEmpty() =>
            Assert.Equal("", TimberbotPure.ExtractAgentStatusString(null));

        [Fact]
        public void JsonNull_ReturnsEmpty() =>
            Assert.Equal("", TimberbotPure.ExtractAgentStatusString(JValue.CreateNull()));

        [Fact]
        public void TopLevelString_Lowercased() =>
            Assert.Equal("running", TimberbotPure.ExtractAgentStatusString(new JValue("RUNNING")));

        [Fact]
        public void ObjectWithStatus_ExtractsAndLowercases() =>
            Assert.Equal("thinking",
                TimberbotPure.ExtractAgentStatusString(JObject.Parse("{\"status\":\"Thinking\"}")));

        [Fact]
        public void ObjectWithoutStatus_ReturnsEmpty() =>
            Assert.Equal("",
                TimberbotPure.ExtractAgentStatusString(JObject.Parse("{\"other\":\"x\"}")));

        [Fact]
        public void EmptyObject_ReturnsEmpty() =>
            Assert.Equal("",
                TimberbotPure.ExtractAgentStatusString(new JObject()));
    }

    public class ClassifyConnectionTests
    {
        // No state poll yet -> always Disconnected, gate off.
        [Fact]
        public void PollFailed_Disconnected()
        {
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(false, null);
            Assert.Equal(TimberbotPure.ConnectionPillState.Disconnected, pill);
            Assert.False(gateOn);
        }

        // pollOk=true but null state should still classify as Disconnected — the
        // server gave us nothing useful.
        [Fact]
        public void NullState_Disconnected()
        {
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, null);
            Assert.Equal(TimberbotPure.ConnectionPillState.Disconnected, pill);
            Assert.False(gateOn);
        }

        [Fact]
        public void ReadyFalse_NotReady()
        {
            var state = JObject.Parse("{\"ready\":false}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.NotReady, pill);
            Assert.False(gateOn);
        }

        [Fact]
        public void ReadyTrue_IdleAgent_Idle()
        {
            var state = JObject.Parse("{\"ready\":true,\"agentStatus\":\"idle\"}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.Idle, pill);
            Assert.True(gateOn);
        }

        [Fact]
        public void ReadyTrue_PendingRequest_Running()
        {
            var state = JObject.Parse("{\"ready\":true,\"pendingRequest\":{\"id\":1,\"prompt\":\"hi\"}}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.Running, pill);
            Assert.True(gateOn);
        }

        [Fact]
        public void ReadyTrue_AgentBusy_Running()
        {
            var state = JObject.Parse("{\"ready\":true,\"agentStatus\":\"thinking\"}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.Running, pill);
            Assert.True(gateOn);
        }

        // The lastError path must keep gateOn aligned with `ready` so the
        // player can still click Stop to bail out of a stuck cycle. This was
        // the key opencode-review finding.
        [Fact]
        public void Error_WhileReady_KeepsGateOn()
        {
            var state = JObject.Parse("{\"ready\":true,\"lastError\":\"boom\"}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.Error, pill);
            Assert.True(gateOn);
        }

        [Fact]
        public void Error_WhileNotReady_GateOff()
        {
            var state = JObject.Parse("{\"ready\":false,\"lastError\":\"boom\"}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.Error, pill);
            Assert.False(gateOn);
        }

        // Missing `ready` field is treated as false (gate off, NotReady).
        [Fact]
        public void MissingReady_TreatedAsNotReady()
        {
            var state = JObject.Parse("{\"mode\":\"autonomous\"}");
            var (pill, gateOn) = TimberbotPure.ClassifyConnection(true, state);
            Assert.Equal(TimberbotPure.ConnectionPillState.NotReady, pill);
            Assert.False(gateOn);
        }
    }
}
