// TimberbotAgentStateTests.cs - serialization, ack lifecycle, gate carve-out,
// and webhook URL clearing on heartbeat lapse for TimberbotAgentState. This is
// the foundation container for the mod ↔ connector architecture rework
// (issue #13); the tests run pure (no Unity/Timberborn deps).

using System;
using Newtonsoft.Json.Linq;
using Xunit;

namespace Timberbot.Tests
{
    public class TimberbotAgentStateTests
    {
        // --- defaults & basic mutators -----------------------------------

        [Fact]
        public void Defaults_MatchSpec()
        {
            var s = new TimberbotAgentState();
            Assert.Equal(TimberbotAgentState.DefaultMode, s.Mode);
            Assert.Equal("request", s.Mode);
            Assert.Equal("", s.Goal);
            Assert.Null(s.LastError);
            Assert.False(s.Ready);
            Assert.Null(s.PendingRequest);
            Assert.Null(s.TbotWebhookUrl);
            Assert.Equal(0, s.LastAckedRequestId);
        }

        [Theory]
        [InlineData("autonomous", true)]
        [InlineData("request", true)]
        [InlineData("AUTONOMOUS", false)] // case-sensitive on purpose
        [InlineData("", false)]
        [InlineData(null, false)]
        [InlineData("rogue", false)]
        public void SetMode_RejectsInvalidValues(string value, bool ok)
        {
            var s = new TimberbotAgentState();
            Assert.Equal(ok, s.SetMode(value));
            if (ok) Assert.Equal(value, s.Mode);
            else Assert.Equal(TimberbotAgentState.DefaultMode, s.Mode);
        }

        [Fact]
        public void SetGoal_NullCoercesToEmpty()
        {
            var s = new TimberbotAgentState();
            s.SetGoal(null);
            Assert.Equal("", s.Goal);
            s.SetGoal("reach 50 beavers");
            Assert.Equal("reach 50 beavers", s.Goal);
        }

        // --- pending request slot ----------------------------------------

        [Fact]
        public void EnqueueRequest_AllocatesMonotonicIds()
        {
            var s = new TimberbotAgentState();
            int a = s.EnqueueRequest("first");
            int b = s.EnqueueRequest("second");
            Assert.Equal(1, a);
            Assert.Equal(2, b);
            var pending = s.PendingRequest;
            Assert.NotNull(pending);
            // Single-slot: second prompt overwrote first.
            Assert.Equal(2, pending.Value.Id);
            Assert.Equal("second", pending.Value.Prompt);
        }

        [Fact]
        public void Heartbeat_ClearsPendingSlotWhenAckedAtOrAhead()
        {
            var s = new TimberbotAgentState();
            int id = s.EnqueueRequest("hello");
            var now = DateTime.UtcNow;
            // Lower ack doesn't clear.
            Assert.False(s.Heartbeat("running", id - 1, now));
            Assert.NotNull(s.PendingRequest);
            // Equal ack clears (acked_request_id >= pendingRequest.id).
            Assert.True(s.Heartbeat("running", id, now));
            Assert.Null(s.PendingRequest);
            // Repeated ack at the same level is a no-op for the slot.
            Assert.False(s.Heartbeat("running", id, now));
        }

        [Fact]
        public void Heartbeat_HigherAckAlsoClearsPendingSlot()
        {
            var s = new TimberbotAgentState();
            int id = s.EnqueueRequest("hello");
            Assert.True(s.Heartbeat("running", id + 50, DateTime.UtcNow));
            Assert.Null(s.PendingRequest);
            Assert.Equal(id + 50, s.LastAckedRequestId);
        }

        [Fact]
        public void Heartbeat_RecordsLatestAgentStatus()
        {
            var s = new TimberbotAgentState();
            s.Heartbeat("planning", 0, DateTime.UtcNow);
            Assert.Equal("planning", s.AgentStatus);
            s.Heartbeat("acting", 0, DateTime.UtcNow);
            Assert.Equal("acting", s.AgentStatus);
            // Empty status is treated as "no update".
            s.Heartbeat("", 0, DateTime.UtcNow);
            Assert.Equal("acting", s.AgentStatus);
        }

        // --- webhook lapse watchdog --------------------------------------

        [Fact]
        public void RegisterTbotWebhook_ResetsHeartbeatTimer()
        {
            var s = new TimberbotAgentState();
            var t0 = DateTime.UtcNow;
            s.RegisterTbotWebhook("http://localhost:9999/push", t0);
            Assert.Equal("http://localhost:9999/push", s.TbotWebhookUrl);
            // Within the timeout: URL persists.
            Assert.False(s.ExpireWebhookIfStale(t0 + TimeSpan.FromSeconds(5)));
            Assert.Equal("http://localhost:9999/push", s.TbotWebhookUrl);
        }

        [Fact]
        public void ExpireWebhookIfStale_ClearsAfterTimeout()
        {
            var s = new TimberbotAgentState();
            var t0 = DateTime.UtcNow;
            s.RegisterTbotWebhook("http://localhost:9999/push", t0);
            // Past the 6-second timeout.
            var t1 = t0 + TimberbotAgentState.HeartbeatTimeout + TimeSpan.FromMilliseconds(1);
            Assert.True(s.ExpireWebhookIfStale(t1));
            Assert.Null(s.TbotWebhookUrl);
            // Idempotent: a second expiry call returns false (nothing to clear).
            Assert.False(s.ExpireWebhookIfStale(t1));
        }

        [Fact]
        public void Heartbeat_KeepsWebhookAlive()
        {
            var s = new TimberbotAgentState();
            var t0 = DateTime.UtcNow;
            s.RegisterTbotWebhook("http://localhost:9999/push", t0);
            var t1 = t0 + TimeSpan.FromSeconds(5);
            s.Heartbeat("running", 0, t1);  // refresh
            var t2 = t1 + TimeSpan.FromSeconds(5);
            // 10s elapsed since register, but only 5s since heartbeat — alive.
            Assert.False(s.ExpireWebhookIfStale(t2));
            Assert.Equal("http://localhost:9999/push", s.TbotWebhookUrl);
        }

        // --- persistence -------------------------------------------------

        [Fact]
        public void ToJson_OnlySerializesPersistedFields()
        {
            var s = new TimberbotAgentState();
            s.SetMode("autonomous");
            s.SetGoal("test goal");
            s.SetLastError("boom");
            // Ephemerals set: these must NOT appear in the json blob.
            s.SetReady(true);
            s.EnqueueRequest("scratch");
            s.RegisterTbotWebhook("http://localhost:9999", DateTime.UtcNow);

            var json = JObject.Parse(s.ToJson());
            Assert.Equal("autonomous", json.Value<string>("mode"));
            Assert.Equal("test goal", json.Value<string>("goal"));
            Assert.Equal("boom", json.Value<string>("lastError"));
            Assert.Null(json["ready"]);
            Assert.Null(json["pendingRequest"]);
            Assert.Null(json["tbotWebhookUrl"]);
            Assert.Null(json["lastAckedRequestId"]);
        }

        [Fact]
        public void LoadJson_RestoresPersistedFields_ResetsEphemerals()
        {
            var s = new TimberbotAgentState();
            // Pre-load state has ephemerals set; LoadJson must wipe them.
            s.SetReady(true);
            s.EnqueueRequest("garbage");
            s.RegisterTbotWebhook("http://localhost:9999", DateTime.UtcNow);

            var ok = s.LoadJson("{\"mode\":\"autonomous\",\"goal\":\"reach 50\",\"lastError\":null}");
            Assert.True(ok);
            Assert.Equal("autonomous", s.Mode);
            Assert.Equal("reach 50", s.Goal);
            Assert.Null(s.LastError);
            Assert.False(s.Ready);  // ephemerals were reset
            Assert.Null(s.PendingRequest);
            Assert.Null(s.TbotWebhookUrl);
            Assert.Equal(0, s.LastAckedRequestId);
        }

        [Fact]
        public void LoadJson_IgnoresInvalidMode()
        {
            var s = new TimberbotAgentState();
            s.LoadJson("{\"mode\":\"bogus\",\"goal\":\"x\"}");
            // mode falls back to the existing value (default = request).
            Assert.Equal(TimberbotAgentState.DefaultMode, s.Mode);
            Assert.Equal("x", s.Goal);
        }

        [Fact]
        public void LoadJson_TolerantsMalformedInput()
        {
            var s = new TimberbotAgentState();
            // empty/whitespace/junk all just return false without throwing.
            Assert.False(s.LoadJson(null));
            Assert.False(s.LoadJson(""));
            Assert.False(s.LoadJson("   "));
            Assert.False(s.LoadJson("{not json"));
            // Defaults preserved.
            Assert.Equal(TimberbotAgentState.DefaultMode, s.Mode);
            Assert.Equal("", s.Goal);
        }

        [Fact]
        public void Roundtrip_PreservesPersistedFields()
        {
            var src = new TimberbotAgentState();
            src.SetMode("autonomous");
            src.SetGoal("two settlements survive winter");
            src.SetLastError("connector missed heartbeat");

            var dst = new TimberbotAgentState();
            dst.LoadJson(src.ToJson());
            Assert.Equal(src.Mode, dst.Mode);
            Assert.Equal(src.Goal, dst.Goal);
            Assert.Equal(src.LastError, dst.LastError);
        }

        [Fact]
        public void ResetEphemerals_LeavesPersistedAlone()
        {
            var s = new TimberbotAgentState();
            s.SetMode("autonomous");
            s.SetGoal("eat trees");
            s.SetLastError("err");
            s.SetReady(true);
            s.EnqueueRequest("scratch");
            s.RegisterTbotWebhook("http://x", DateTime.UtcNow);

            s.ResetEphemerals();

            Assert.Equal("autonomous", s.Mode);
            Assert.Equal("eat trees", s.Goal);
            Assert.Equal("err", s.LastError);
            Assert.False(s.Ready);
            Assert.Null(s.PendingRequest);
            Assert.Null(s.TbotWebhookUrl);
        }

        // --- state response shape ----------------------------------------

        [Fact]
        public void ToStateResponseJson_IncludesAllSixFields()
        {
            var s = new TimberbotAgentState();
            s.SetMode("request");
            s.SetGoal("g");
            s.SetReady(true);
            s.Heartbeat("running", 0, DateTime.UtcNow);
            int id = s.EnqueueRequest("hi");
            var json = JObject.Parse(s.ToStateResponseJson());
            Assert.Equal("request", json.Value<string>("mode"));
            Assert.Equal("g", json.Value<string>("goal"));
            Assert.True(json.Value<bool>("ready"));
            Assert.Equal("running", json.Value<string>("agentStatus"));
            Assert.Null(json["lastError"].ToObject<string>());
            var pending = (JObject)json["pendingRequest"];
            Assert.Equal(id, pending.Value<int>("id"));
            Assert.Equal("hi", pending.Value<string>("prompt"));
        }

        [Fact]
        public void ToStateResponseJson_NullPendingWhenSlotEmpty()
        {
            var s = new TimberbotAgentState();
            var json = JObject.Parse(s.ToStateResponseJson());
            Assert.True(json.ContainsKey("pendingRequest"));
            Assert.Equal(JTokenType.Null, json["pendingRequest"].Type);
        }

        // --- gate carve-out ----------------------------------------------

        [Theory]
        [InlineData("/api/ping", true)]
        [InlineData("/api/ready", true)]
        [InlineData("/api/agent", true)]
        [InlineData("/api/agent/state", true)]
        [InlineData("/api/agent/config", true)]
        [InlineData("/api/agent/request", true)]
        [InlineData("/api/tbot", true)]
        [InlineData("/api/tbot/register", true)]
        [InlineData("/api/tbot/heartbeat", true)]
        // Non-whitelisted /api/* paths are gated.
        [InlineData("/api/buildings", false)]
        [InlineData("/api/summary", false)]
        [InlineData("/api/building/pause", false)]
        [InlineData("/api/webhooks", false)]
        [InlineData("/api/settlement", false)]
        // Random / unrelated paths.
        [InlineData("", false)]
        [InlineData(null, false)]
        [InlineData("/", false)]
        public void IsGateExempt_MatchesWhitelist(string path, bool expected)
        {
            Assert.Equal(expected, TimberbotAgentState.IsGateExempt(path));
        }

        [Fact]
        public void GameNotReadyJson_MatchesSpecShape()
        {
            var parsed = JObject.Parse(TimberbotAgentState.GameNotReadyJson);
            Assert.Equal("game_not_ready", parsed.Value<string>("error"));
            Assert.False(string.IsNullOrEmpty(parsed.Value<string>("hint")));
        }
    }
}
