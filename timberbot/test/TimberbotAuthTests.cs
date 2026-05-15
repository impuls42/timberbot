// TimberbotAuthTests.cs - unit tests for bearer-token auth helpers.
//
// The HTTP listener / refuse-to-start guard live in Unity-bound classes
// (TimberbotHttpServer, TimberbotService) that the test project deliberately
// doesn't compile. The pure helpers exercised here capture the entire
// authentication contract:
//
//   - RequiresAuthToken           — refuse-to-start invariant
//   - IsLoopbackAddress           — the loopback set the guard checks against
//   - ExtractBearerToken          — Authorization-header parsing
//   - BearerTokenMatches          — constant-time compare via CryptographicOperations.FixedTimeEquals
//
// The middleware in TimberbotHttpServer.cs is a straight call chain over
// these helpers, so covering the helpers covers the contract.

using System;
using System.Diagnostics;
using Xunit;
using Timberbot;

namespace Timberbot.Tests
{
    public class RequiresAuthTokenTests
    {
        [Theory]
        [InlineData("localhost", "")]
        [InlineData("127.0.0.1", "")]
        [InlineData("::1", "")]
        [InlineData("LocalHost", "")]
        [InlineData(" 127.0.0.1 ", "")]
        public void Loopback_EmptyToken_NotRequired(string addr, string token)
        {
            Assert.False(TimberbotPure.RequiresAuthToken(addr, token));
        }

        [Theory]
        [InlineData("0.0.0.0")]
        [InlineData("+")]
        [InlineData("*")]
        [InlineData("10.0.0.1")]
        [InlineData("192.168.1.5")]
        [InlineData("example.com")]
        public void NonLoopback_EmptyToken_Required(string addr)
        {
            Assert.True(TimberbotPure.RequiresAuthToken(addr, ""));
            Assert.True(TimberbotPure.RequiresAuthToken(addr, "   "));
            Assert.True(TimberbotPure.RequiresAuthToken(addr, null));
        }

        [Theory]
        [InlineData("0.0.0.0")]
        [InlineData("10.0.0.1")]
        [InlineData("example.com")]
        public void NonLoopback_WithToken_NotRequired(string addr)
        {
            Assert.False(TimberbotPure.RequiresAuthToken(addr, "s3cret"));
        }

        [Fact]
        public void EmptyAddress_TreatedAsLoopback()
        {
            // Empty / null listenAddress falls through to localhost in
            // TimberbotHttpServer (matches its default), so it's safe.
            Assert.False(TimberbotPure.RequiresAuthToken(null, ""));
            Assert.False(TimberbotPure.RequiresAuthToken("", ""));
            Assert.False(TimberbotPure.RequiresAuthToken("   ", ""));
        }
    }

    public class IsLoopbackAddressTests
    {
        [Theory]
        [InlineData("localhost", true)]
        [InlineData("127.0.0.1", true)]
        [InlineData("::1", true)]
        [InlineData("LOCALHOST", true)]
        [InlineData("  127.0.0.1  ", true)]
        [InlineData("0.0.0.0", false)]
        [InlineData("+", false)]
        [InlineData("*", false)]
        [InlineData("10.0.0.1", false)]
        [InlineData("example.com", false)]
        [InlineData("", true)]
        [InlineData(null, true)]
        public void Detection(string addr, bool expected)
        {
            Assert.Equal(expected, TimberbotPure.IsLoopbackAddress(addr));
        }
    }

    public class ExtractBearerTokenTests
    {
        [Fact]
        public void NullHeader_ReturnsNull() =>
            Assert.Null(TimberbotPure.ExtractBearerToken(null));

        [Fact]
        public void EmptyHeader_ReturnsNull() =>
            Assert.Null(TimberbotPure.ExtractBearerToken(""));

        [Fact]
        public void WhitespaceHeader_ReturnsNull() =>
            Assert.Null(TimberbotPure.ExtractBearerToken("   "));

        [Fact]
        public void BearerOnly_ReturnsNull() =>
            Assert.Null(TimberbotPure.ExtractBearerToken("Bearer"));

        [Fact]
        public void BearerSpace_NoToken_ReturnsNull() =>
            Assert.Null(TimberbotPure.ExtractBearerToken("Bearer "));

        [Fact]
        public void BasicScheme_ReturnsNull() =>
            Assert.Null(TimberbotPure.ExtractBearerToken("Basic dXNlcjpwYXNz"));

        [Fact]
        public void ValidBearer_ReturnsToken() =>
            Assert.Equal("abc123", TimberbotPure.ExtractBearerToken("Bearer abc123"));

        [Fact]
        public void CaseInsensitiveScheme() =>
            Assert.Equal("abc123", TimberbotPure.ExtractBearerToken("bearer abc123"));

        [Fact]
        public void ExtraWhitespace_Trimmed() =>
            Assert.Equal("abc123", TimberbotPure.ExtractBearerToken("Bearer   abc123   "));

        [Fact]
        public void TokenWithSpecialChars_Preserved() =>
            Assert.Equal("a.b-c_d~e", TimberbotPure.ExtractBearerToken("Bearer a.b-c_d~e"));
    }

    public class BearerTokenMatchesTests
    {
        [Fact]
        public void Match_ReturnsTrue() =>
            Assert.True(TimberbotPure.BearerTokenMatches("s3cret", "s3cret"));

        [Fact]
        public void Mismatch_ReturnsFalse() =>
            Assert.False(TimberbotPure.BearerTokenMatches("s3cret", "wrong"));

        [Fact]
        public void DifferentLengths_ReturnsFalse() =>
            Assert.False(TimberbotPure.BearerTokenMatches("short", "longer-token"));

        [Fact]
        public void EmptyExpected_ReturnsFalse() =>
            Assert.False(TimberbotPure.BearerTokenMatches("", "anything"));

        [Fact]
        public void EmptyPresented_ReturnsFalse() =>
            Assert.False(TimberbotPure.BearerTokenMatches("s3cret", ""));

        [Fact]
        public void NullExpected_ReturnsFalse() =>
            Assert.False(TimberbotPure.BearerTokenMatches(null, "s3cret"));

        [Fact]
        public void NullPresented_ReturnsFalse() =>
            Assert.False(TimberbotPure.BearerTokenMatches("s3cret", null));

        [Fact]
        public void CaseSensitive() =>
            Assert.False(TimberbotPure.BearerTokenMatches("S3cret", "s3cret"));

        [Fact]
        public void UnicodeToken_RoundTrips() =>
            Assert.True(TimberbotPure.BearerTokenMatches("résumé-🔒", "résumé-🔒"));

        // Sanity check: the underlying primitive is
        // CryptographicOperations.FixedTimeEquals, which is constant-time over
        // the byte length. We can't make a hard timing-side-channel assertion
        // in a portable unit test (JIT warmup, GC, scheduler jitter all
        // dominate), but we can at least verify the helper does not bail out
        // on first byte mismatch. Pairs of equal-length tokens that mismatch
        // at position 0 vs at the last byte should take indistinguishable
        // time on average. We just assert both paths return false — the
        // FixedTimeEquals contract is tested by .NET upstream.
        [Fact]
        public void FixedTime_BothMismatchPositionsReturnFalse()
        {
            // 32-byte tokens (typical for a HMAC-SHA256-shaped secret).
            var expected = new string('a', 32);
            var earlyMismatch = "b" + new string('a', 31);
            var lateMismatch = new string('a', 31) + "b";
            Assert.False(TimberbotPure.BearerTokenMatches(expected, earlyMismatch));
            Assert.False(TimberbotPure.BearerTokenMatches(expected, lateMismatch));
        }

        // Best-effort smoke test for constant-time-ish behavior. Highly
        // noisy under unit-test conditions, so we only assert ratios that
        // would fail loudly if someone replaced FixedTimeEquals with a naive
        // memcmp/string.Equals fast-bail. We give a generous 20x tolerance
        // so this stays green on slow CI.
        [Fact]
        public void NoEarlyExitOnFirstByteMismatch()
        {
            var expected = new string('a', 4096);
            var earlyMismatch = "b" + new string('a', 4095);
            var lateMismatch = new string('a', 4095) + "b";

            const int iterations = 5000;
            var sw = Stopwatch.StartNew();
            for (int i = 0; i < iterations; i++)
                TimberbotPure.BearerTokenMatches(expected, earlyMismatch);
            var earlyTicks = sw.ElapsedTicks;

            sw.Restart();
            for (int i = 0; i < iterations; i++)
                TimberbotPure.BearerTokenMatches(expected, lateMismatch);
            var lateTicks = sw.ElapsedTicks;

            // If a naive compare leaked timing, earlyTicks would be tiny vs
            // lateTicks. A 20x ratio is well outside the noise floor of any
            // CI environment but well inside the orders-of-magnitude gap an
            // early-exit memcmp would produce on 4 KB inputs.
            var ratio = (double)Math.Max(earlyTicks, lateTicks) / Math.Max(1, Math.Min(earlyTicks, lateTicks));
            Assert.True(ratio < 20.0,
                $"early-mismatch={earlyTicks} ticks, late-mismatch={lateTicks} ticks (ratio={ratio:F2}). " +
                "Comparison is not constant-time enough — should use CryptographicOperations.FixedTimeEquals.");
        }
    }
}
