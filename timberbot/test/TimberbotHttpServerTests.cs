// TimberbotHttpServerTests.cs - source-level checks for the ready-gate
// middleware and the openapi entries that document it. We can't instantiate
// TimberbotHttpServer in xUnit (it pulls Unity + Bindito), so the tests scan
// the .cs source and openapi.yaml the same way OpenApiContractTests does.
//
// What we pin here:
//   1. Every new agent/connector route is registered in TimberbotHttpServer.
//   2. The ready-gate middleware is present and runs *before* the inline
//      shortcuts and the POST queue.
//   3. The 409 carve-out whitelist (/api/agent/*, /api/ready, /api/tbot/*,
//      /api/ping) is structurally enforced via TimberbotAgentState.IsGateExempt
//      so a future edit to the path list can't accidentally widen the gate.
//   4. The 409 wire shape lines up between the C# constant and the openapi
//      response schema.

using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Timberbot.Tests
{
    public class TimberbotHttpServerTests
    {
        // Agent/widget HTTP surface that survives after the WS rework
        // (issue #28). `/api/tbot/*` is gone — connectors talk WebSocket
        // now. All listed routes MUST be wired in TimberbotHttpServer.cs and
        // documented in openapi.yaml.
        private static readonly (string Path, string Method)[] AgentConnectorRoutes =
        {
            ("/api/agent/state", "GET"),
            ("/api/agent/config", "POST"),
            ("/api/agent/request", "POST"),
            ("/api/ready", "POST"),
        };

        // Routes deleted in the WS rework. They MUST NOT reappear in
        // TimberbotHttpServer.cs or openapi.yaml.
        private static readonly string[] DeletedHttpRoutes =
        {
            "/api/tbot/register",
            "/api/tbot/heartbeat",
            "/api/webhooks",
            "/api/webhooks/delete",
        };

        private static string TestArtifact(string name)
        {
            var asmDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";
            return Path.Combine(asmDir, name);
        }

        private static string LoadHttpServerSource() =>
            File.ReadAllText(TestArtifact("TimberbotHttpServer.cs"));

        private static YamlMappingNode LoadSpec()
        {
            using var sr = new StreamReader(TestArtifact("openapi.yaml"));
            var yaml = new YamlStream();
            yaml.Load(sr);
            return (YamlMappingNode)yaml.Documents[0].RootNode;
        }

        // --- new routes are wired ----------------------------------------

        [Fact]
        public void AgentConnectorRoutes_AllWiredInHttpServer()
        {
            var src = LoadHttpServerSource();
            var missing = AgentConnectorRoutes
                .Where(r => !RouteAppearsInSource(src, r.Path, r.Method))
                .Select(r => $"{r.Method} {r.Path}")
                .ToList();
            Assert.True(missing.Count == 0,
                "Agent/connector routes missing from TimberbotHttpServer.cs:\n  " +
                string.Join("\n  ", missing));
        }

        // GET routes are matched via `path == "/api/..."` inline blocks, POSTs
        // via `Queued("/api/...", ...)` registrations. Mirrors the regex
        // strategy in OpenApiContractTests.
        private static bool RouteAppearsInSource(string source, string path, string method)
        {
            if (method == "GET")
            {
                var inline = new Regex("path\\s*==\\s*\"" + Regex.Escape(path) + "\"");
                return inline.IsMatch(source);
            }
            var queued = new Regex("Queued\\s*\\(\\s*\"" + Regex.Escape(path) + "\"");
            return queued.IsMatch(source);
        }

        // --- ready-gate middleware is wired ------------------------------

        [Fact]
        public void ReadyGate_IsInstalledBeforeRouteDispatch()
        {
            var src = LoadHttpServerSource();
            // Marker phrases from the gate-check block in the listener loop.
            Assert.Contains("TimberbotAgentState.IsGateExempt", src);
            Assert.Contains("AgentState.Ready", src);
            Assert.Contains("GameNotReadyJson", src);

            // The gate must run before the inline /api/ping handler so a
            // non-whitelisted POST that arrives while ready=false can't slip
            // through into the queue. (Note: auth-check carves /api/ping out
            // earlier in the loop with `path != "/api/ping"`; we look for the
            // equality form to find the actual handler dispatch.)
            int gateIdx = src.IndexOf("TimberbotAgentState.IsGateExempt", StringComparison.Ordinal);
            int pingHandlerIdx = src.IndexOf("path == \"/api/ping\"", StringComparison.Ordinal);
            Assert.InRange(gateIdx, 0, int.MaxValue);
            Assert.InRange(pingHandlerIdx, 0, int.MaxValue);
            Assert.True(gateIdx < pingHandlerIdx,
                "Ready-gate middleware must run before the inline /api/ping handler.");
        }

        [Fact]
        public void ReadyGate_RespondsWith409()
        {
            var src = LoadHttpServerSource();
            // A literal 409 with the GameNotReadyJson body is the gate response.
            Assert.Matches(new Regex("Respond\\([^,]+,\\s*409,\\s*TimberbotAgentState\\.GameNotReadyJson"), src);
        }

        // --- carve-out whitelist semantics --------------------------------

        [Theory]
        // Gated /api/* paths return 409 while ready=false.
        [InlineData("/api/buildings", false)]
        [InlineData("/api/building/pause", false)]
        [InlineData("/api/summary", false)]
        [InlineData("/api/settlement", false)]
        // /api/tbot/* was deleted entirely; if some future commit re-added
        // the path it must not be on the whitelist either.
        [InlineData("/api/tbot/register", false)]
        [InlineData("/api/tbot/heartbeat", false)]
        // Whitelist: /api/ping, /api/ready, /api/agent/*.
        [InlineData("/api/ping", true)]
        [InlineData("/api/ready", true)]
        [InlineData("/api/agent/state", true)]
        [InlineData("/api/agent/config", true)]
        [InlineData("/api/agent/request", true)]
        public void Whitelist_PinsTheCarveOut(string path, bool exempt)
        {
            Assert.Equal(exempt, TimberbotAgentState.IsGateExempt(path));
        }

        [Fact]
        public void Whitelist_RejectsLookalikePaths()
        {
            // Intentional negative cases: a confused client/proxy must not
            // sneak through the gate via a near-miss path.
            Assert.False(TimberbotAgentState.IsGateExempt("/api/agentstate"));
            Assert.False(TimberbotAgentState.IsGateExempt("/api/agent-state"));
            Assert.False(TimberbotAgentState.IsGateExempt("/api/readyish"));
            Assert.False(TimberbotAgentState.IsGateExempt("/api/pingg"));
            // No /api/ prefix => definitely not exempt.
            Assert.False(TimberbotAgentState.IsGateExempt("/agent/state"));
        }

        // --- deleted routes must stay gone --------------------------------

        [Fact]
        public void DeletedHttpRoutes_NotInHttpServerSource()
        {
            var src = LoadHttpServerSource();
            var leaked = DeletedHttpRoutes
                .Where(path =>
                    new Regex("Queued\\s*\\(\\s*\"" + Regex.Escape(path) + "\"").IsMatch(src) ||
                    new Regex("path\\s*==\\s*\"" + Regex.Escape(path) + "\"").IsMatch(src))
                .ToList();
            Assert.True(leaked.Count == 0,
                "TimberbotHttpServer.cs still references deleted routes:\n  " +
                string.Join("\n  ", leaked));
        }

        [Fact]
        public void DeletedHttpRoutes_NotInSpec()
        {
            var spec = LoadSpec();
            var paths = (YamlMappingNode)spec.Children[new YamlScalarNode("paths")];
            var leaked = DeletedHttpRoutes
                .Where(path => paths.Children.ContainsKey(new YamlScalarNode(path)))
                .ToList();
            Assert.True(leaked.Count == 0,
                "openapi.yaml still declares deleted routes:\n  " +
                string.Join("\n  ", leaked));
        }

        // --- openapi alignment for the new routes -------------------------

        [Fact]
        public void Spec_DocumentsAllAgentConnectorRoutes()
        {
            var spec = LoadSpec();
            var paths = (YamlMappingNode)spec.Children[new YamlScalarNode("paths")];
            foreach (var (route, method) in AgentConnectorRoutes)
            {
                Assert.True(paths.Children.TryGetValue(new YamlScalarNode(route), out var node),
                    $"openapi.yaml does not declare path {route}");
                var pathItem = (YamlMappingNode)node;
                Assert.True(pathItem.Children.ContainsKey(new YamlScalarNode(method.ToLowerInvariant())),
                    $"openapi.yaml declares {route} but not the {method} operation");
            }
        }

        [Fact]
        public void Spec_DeclaresGameNotReadyResponseShape()
        {
            var spec = LoadSpec();
            var components = (YamlMappingNode)spec.Children[new YamlScalarNode("components")];
            var schemas = (YamlMappingNode)components.Children[new YamlScalarNode("schemas")];
            Assert.True(schemas.Children.ContainsKey(new YamlScalarNode("GameNotReady")),
                "openapi.yaml is missing the GameNotReady schema used by the gate response.");
            var responses = (YamlMappingNode)components.Children[new YamlScalarNode("responses")];
            Assert.True(responses.Children.ContainsKey(new YamlScalarNode("GameNotReady")),
                "openapi.yaml is missing the GameNotReady response wrapper.");
        }

        [Fact]
        public void GameNotReadyJson_AndSpec_AgreeOnTheWireShape()
        {
            // The C# constant must satisfy `required: [error, hint]` and the
            // enum on `error`.
            var body = JObject.Parse(TimberbotAgentState.GameNotReadyJson);
            Assert.Equal("game_not_ready", body.Value<string>("error"));
            Assert.False(string.IsNullOrEmpty(body.Value<string>("hint")));

            var spec = LoadSpec();
            var schemas = (YamlMappingNode)((YamlMappingNode)spec.Children[new YamlScalarNode("components")])
                .Children[new YamlScalarNode("schemas")];
            var gnr = (YamlMappingNode)schemas.Children[new YamlScalarNode("GameNotReady")];
            var required = (YamlSequenceNode)gnr.Children[new YamlScalarNode("required")];
            var requiredNames = required.Children.OfType<YamlScalarNode>().Select(n => n.Value).ToList();
            Assert.Contains("error", requiredNames);
            Assert.Contains("hint", requiredNames);
        }
    }
}
