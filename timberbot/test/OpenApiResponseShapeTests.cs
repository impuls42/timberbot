// OpenApiResponseShapeTests.cs - validate that the JSON fixtures captured
// from a live mod under `python/tests/fixtures/openapi/` actually satisfy
// the `required` constraints documented in openapi.yaml's 200-response
// schemas. This is the C# half of the round-trip; the Python half
// (tests/contract/test_openapi_responses.py) validates the inverse
// (fixture parses through the generated Pydantic models).
//
// The walker resolves `$ref` references inside `components/schemas` and
// merges `allOf` compositions so the paginated envelopes (BuildingList,
// BeaverList, ...) inherit `total/offset/limit/items` from
// `PaginatedEnvelope`.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Newtonsoft.Json.Linq;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Timberbot.Tests
{
    public class OpenApiResponseShapeTests
    {
        private static string TestArtifact(string name)
        {
            var asmDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";
            return Path.Combine(asmDir, name);
        }

        private static YamlMappingNode LoadSpec()
        {
            using var sr = new StreamReader(TestArtifact("openapi.yaml"));
            var yaml = new YamlStream();
            yaml.Load(sr);
            return (YamlMappingNode)yaml.Documents[0].RootNode;
        }

        // Map operationId -> fixture filename (under bin/.../fixtures/openapi/).
        // Mirrors RESPONSE_MAP in the Python contract test.
        private static readonly Dictionary<string, string> OperationFixtures = new Dictionary<string, string>
        {
            ["ping"] = "ping.json",
            ["settlement"] = "settlement.json",
            ["summary"] = "summary.json",
            ["time"] = "time.json",
            ["weather"] = "weather.json",
            ["population"] = "population.json",
            ["resources"] = "resources.json",
            ["districts"] = "districts.json",
            ["distribution"] = "distribution.json",
            ["science"] = "science.json",
            ["wellbeing"] = "wellbeing.json",
            ["workhours"] = "workhours.json",
            ["speed"] = "speed.json",
            ["prefabs"] = "prefabs.json",
            ["power"] = "power.json",
            ["tiles"] = "tiles.json",
            ["tree_clusters"] = "tree_clusters.json",
            ["food_clusters"] = "food_clusters.json",
            ["alerts"] = "alerts.json",
            ["notifications"] = "notifications.json",
            ["buildings"] = "buildings.json",
            ["beavers"] = "beavers.json",
            ["trees"] = "trees.json",
            ["crops"] = "crops.json",
            ["gatherables"] = "gatherables.json",
            ["list_webhooks"] = "list_webhooks.json",
        };

        public static IEnumerable<object[]> FixtureCases =>
            OperationFixtures.Keys.OrderBy(k => k).Select(k => new object[] { k });

        [Theory]
        [MemberData(nameof(FixtureCases))]
        public void Fixture_SatisfiesSpecRequiredFields(string operationId)
        {
            var spec = LoadSpec();
            var responseSchema = FindResponseSchemaFor(spec, operationId);
            Assert.NotNull(responseSchema);

            var resolved = ResolveSchema(spec, responseSchema!);
            var fixturePath = TestArtifact(Path.Combine("fixtures", "openapi", OperationFixtures[operationId]));
            Assert.True(File.Exists(fixturePath),
                $"fixture missing for {operationId} at {fixturePath}. " +
                "Run python/scripts/capture_fixtures.py against a live mod.");
            var fixture = JToken.Parse(File.ReadAllText(fixturePath));

            var missing = new List<string>();
            ValidateAgainstSchema(spec, resolved, fixture, path: operationId, missing);
            Assert.True(missing.Count == 0,
                $"Fixture {operationId}.json is missing fields the spec marks `required`:\n  " +
                string.Join("\n  ", missing));
        }

        [Fact]
        public void EveryGetOperation_HasAFixtureMapping()
        {
            var spec = LoadSpec();
            var getOps = new List<string>();
            var paths = (YamlMappingNode)spec.Children[new YamlScalarNode("paths")];
            foreach (var kvp in paths.Children)
            {
                var pathItem = (YamlMappingNode)kvp.Value;
                if (pathItem.Children.TryGetValue(new YamlScalarNode("get"), out var getNode))
                {
                    var op = (YamlMappingNode)getNode;
                    if (op.Children.TryGetValue(new YamlScalarNode("operationId"), out var idNode))
                        getOps.Add(((YamlScalarNode)idNode).Value!);
                }
            }
            var missing = getOps.Except(OperationFixtures.Keys).OrderBy(x => x).ToList();
            Assert.True(missing.Count == 0,
                "GET operations declared in openapi.yaml but unmapped in OperationFixtures:\n  " +
                string.Join("\n  ", missing));
        }

        // -- spec navigation -------------------------------------------------

        private static YamlMappingNode? FindResponseSchemaFor(YamlMappingNode spec, string operationId)
        {
            var paths = (YamlMappingNode)spec.Children[new YamlScalarNode("paths")];
            foreach (var kvp in paths.Children)
            {
                var pathItem = (YamlMappingNode)kvp.Value;
                foreach (var methodKvp in pathItem.Children)
                {
                    var op = methodKvp.Value as YamlMappingNode;
                    if (op == null) continue;
                    if (!op.Children.TryGetValue(new YamlScalarNode("operationId"), out var idNode)) continue;
                    if (((YamlScalarNode)idNode).Value != operationId) continue;

                    if (!op.Children.TryGetValue(new YamlScalarNode("responses"), out var respNode)) return null;
                    var responses = (YamlMappingNode)respNode;
                    if (!responses.Children.TryGetValue(new YamlScalarNode("200"), out var twoHundred)) return null;
                    var ok = (YamlMappingNode)twoHundred;
                    if (!ok.Children.TryGetValue(new YamlScalarNode("content"), out var contentNode)) return null;
                    var content = (YamlMappingNode)contentNode;
                    if (!content.Children.TryGetValue(new YamlScalarNode("application/json"), out var appJsonNode)) return null;
                    var appJson = (YamlMappingNode)appJsonNode;
                    if (!appJson.Children.TryGetValue(new YamlScalarNode("schema"), out var schemaNode)) return null;
                    return (YamlMappingNode)schemaNode;
                }
            }
            return null;
        }

        // Resolve a schema node by chasing $ref into components/schemas and
        // collapsing top-level `allOf` into a single merged object schema.
        private static YamlMappingNode ResolveSchema(YamlMappingNode spec, YamlMappingNode schema)
        {
            // Chase $ref
            while (schema.Children.TryGetValue(new YamlScalarNode("$ref"), out var refNode))
            {
                var refStr = ((YamlScalarNode)refNode).Value!;
                schema = DerefLocal(spec, refStr);
            }

            // Merge allOf (left-to-right). The semantics are intersection, but
            // for required-field checking we treat it as union of properties
            // and required arrays. items overrides come through later branches
            // and replace previous items.
            if (schema.Children.TryGetValue(new YamlScalarNode("allOf"), out var allOfNode))
            {
                var merged = new YamlMappingNode();
                merged.Children[new YamlScalarNode("type")] = new YamlScalarNode("object");
                var mergedProps = new YamlMappingNode();
                var requiredItems = new List<YamlNode>();
                YamlNode? itemsOverride = null;
                foreach (var part in (YamlSequenceNode)allOfNode)
                {
                    var resolved = ResolveSchema(spec, (YamlMappingNode)part);
                    if (resolved.Children.TryGetValue(new YamlScalarNode("properties"), out var propsNode))
                    {
                        foreach (var p in ((YamlMappingNode)propsNode).Children)
                            mergedProps.Children[p.Key] = p.Value;
                    }
                    if (resolved.Children.TryGetValue(new YamlScalarNode("required"), out var reqNode))
                    {
                        foreach (var r in (YamlSequenceNode)reqNode)
                            if (!requiredItems.Any(x => YamlNodeEquals(x, r))) requiredItems.Add(r);
                    }
                    if (resolved.Children.TryGetValue(new YamlScalarNode("items"), out var itemsNode))
                        itemsOverride = itemsNode;
                    if (resolved.Children.TryGetValue(new YamlScalarNode("type"), out var typeNode))
                        merged.Children[new YamlScalarNode("type")] = typeNode;
                }
                merged.Children[new YamlScalarNode("properties")] = mergedProps;
                if (requiredItems.Count > 0)
                {
                    var seq = new YamlSequenceNode();
                    foreach (var r in requiredItems) seq.Add(r);
                    merged.Children[new YamlScalarNode("required")] = seq;
                }
                if (itemsOverride != null) merged.Children[new YamlScalarNode("items")] = itemsOverride;
                return merged;
            }
            return schema;
        }

        private static YamlMappingNode DerefLocal(YamlMappingNode spec, string refPath)
        {
            // We only support local refs of the form `#/components/schemas/X`.
            const string prefix = "#/components/schemas/";
            Assert.StartsWith(prefix, refPath);
            var name = refPath.Substring(prefix.Length);
            var components = (YamlMappingNode)spec.Children[new YamlScalarNode("components")];
            var schemas = (YamlMappingNode)components.Children[new YamlScalarNode("schemas")];
            return (YamlMappingNode)schemas.Children[new YamlScalarNode(name)];
        }

        private static bool YamlNodeEquals(YamlNode a, YamlNode b)
        {
            return a is YamlScalarNode sa && b is YamlScalarNode sb && sa.Value == sb.Value;
        }

        // -- shape checks ----------------------------------------------------

        private static void ValidateAgainstSchema(
            YamlMappingNode spec, YamlMappingNode schema, JToken token,
            string path, List<string> missing)
        {
            schema = ResolveSchema(spec, schema);
            var type = schema.Children.TryGetValue(new YamlScalarNode("type"), out var typeNode)
                ? ((YamlScalarNode)typeNode).Value
                : null;

            if (type == "array")
            {
                if (token.Type != JTokenType.Array)
                {
                    missing.Add($"{path}: expected array, got {token.Type}");
                    return;
                }
                if (schema.Children.TryGetValue(new YamlScalarNode("items"), out var itemsNode))
                {
                    var arr = (JArray)token;
                    // Only inspect the first item; the schema is uniform.
                    if (arr.Count > 0)
                        ValidateAgainstSchema(spec, (YamlMappingNode)itemsNode, arr[0], $"{path}[0]", missing);
                }
                return;
            }

            if (type == "object" || schema.Children.ContainsKey(new YamlScalarNode("properties")) ||
                schema.Children.ContainsKey(new YamlScalarNode("required")))
            {
                if (token.Type != JTokenType.Object)
                {
                    missing.Add($"{path}: expected object, got {token.Type}");
                    return;
                }
                var obj = (JObject)token;
                if (schema.Children.TryGetValue(new YamlScalarNode("required"), out var reqNode))
                {
                    foreach (var r in (YamlSequenceNode)reqNode)
                    {
                        var field = ((YamlScalarNode)r).Value!;
                        if (obj[field] == null)
                            missing.Add($"{path}.{field} is required but missing");
                    }
                }
                if (schema.Children.TryGetValue(new YamlScalarNode("properties"), out var propsNode))
                {
                    var props = (YamlMappingNode)propsNode;
                    foreach (var p in props.Children)
                    {
                        var field = ((YamlScalarNode)p.Key).Value!;
                        var child = obj[field];
                        if (child == null) continue; // optional or not present
                        if (p.Value is YamlMappingNode childSchema)
                            ValidateAgainstSchema(spec, childSchema, child, $"{path}.{field}", missing);
                    }
                }
            }
        }
    }
}
