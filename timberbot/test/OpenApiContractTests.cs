// OpenApiContractTests.cs - assert the routes declared in TimberbotHttpServer.cs
// stay 1:1 with /openapi.yaml, and that every POST body field the C# side
// extracts via `req.Body?.Value<T>("fieldname")` appears in the spec's
// requestBody schema.
//
// The test project intentionally avoids compiling TimberbotHttpServer.cs
// (it pulls in Bindito + Unity DLLs that aren't available off-machine). Instead
// we copy the file as content and regex-scan it. This loses the type-system
// guarantee but catches the realistic drift cases: adding a route in code but
// forgetting the spec, or vice versa.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Timberbot.Tests
{
    public class OpenApiContractTests
    {
        // GET routes are declared as `case "/api/<path>":` in RouteReadRequest's
        // switch. /api/ping, /api/settlement, /api/tiles are handled inline above
        // the switch in ListenLoop - count them too.
        private static readonly Regex GetCase = new Regex(
            @"case\s+""(/api/[^""]+)""\s*:", RegexOptions.Compiled);
        private static readonly Regex InlineGet = new Regex(
            @"path\s*==\s*""(/api/[^""]+)""", RegexOptions.Compiled);

        // POST routes are registered via `Queued("/api/<path>", req => ...)`.
        private static readonly Regex PostQueued = new Regex(
            @"Queued\s*\(\s*""(/api/[^""]+)""", RegexOptions.Compiled);

        // Field extractions inside a POST handler: `req.Body?.Value<T>("field")`
        // or `req.Body?["field"]`. The second form is used for arrays/objects.
        private static readonly Regex BodyFieldValue = new Regex(
            @"req\.Body\?\.Value<[^>]+>\(\s*""([^""]+)""\s*\)", RegexOptions.Compiled);
        private static readonly Regex BodyFieldIndex = new Regex(
            @"req\.Body\?\[\s*""([^""]+)""\s*\]", RegexOptions.Compiled);

        // Where the spec + the C# source live, relative to the test assembly.
        private static string TestArtifact(string name)
        {
            var asmDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";
            return Path.Combine(asmDir, name);
        }

        private static YamlMappingNode LoadSpec()
        {
            var path = TestArtifact("openapi.yaml");
            Assert.True(File.Exists(path), $"openapi.yaml not found at {path}. " +
                "Build copies it via the csproj <None Include> step.");
            using var sr = new StreamReader(path);
            var yaml = new YamlStream();
            yaml.Load(sr);
            return (YamlMappingNode)yaml.Documents[0].RootNode;
        }

        private static string LoadHttpServerSource()
        {
            var path = TestArtifact("TimberbotHttpServer.cs");
            Assert.True(File.Exists(path), $"TimberbotHttpServer.cs not found at {path}.");
            return File.ReadAllText(path);
        }

        // Collect (route, methods, operationId) from openapi.yaml#/paths.
        private static IEnumerable<(string Path, string Method, string OperationId, YamlMappingNode Operation)> SpecOperations(YamlMappingNode spec)
        {
            var paths = (YamlMappingNode)spec.Children[new YamlScalarNode("paths")];
            foreach (var kvp in paths.Children)
            {
                var path = ((YamlScalarNode)kvp.Key).Value!;
                var pathItem = (YamlMappingNode)kvp.Value;
                foreach (var methodKvp in pathItem.Children)
                {
                    var method = ((YamlScalarNode)methodKvp.Key).Value!.ToLowerInvariant();
                    if (method != "get" && method != "post" && method != "put" && method != "delete") continue;
                    var op = (YamlMappingNode)methodKvp.Value;
                    var opId = op.Children.TryGetValue(new YamlScalarNode("operationId"), out var idNode)
                        ? ((YamlScalarNode)idNode).Value!
                        : "";
                    yield return (path, method, opId, op);
                }
            }
        }

        private static (HashSet<string> Gets, HashSet<string> Posts) SourceRoutes(string source)
        {
            var gets = new HashSet<string>(StringComparer.Ordinal);
            var posts = new HashSet<string>(StringComparer.Ordinal);

            foreach (Match m in GetCase.Matches(source))
                gets.Add(m.Groups[1].Value);
            foreach (Match m in InlineGet.Matches(source))
                gets.Add(m.Groups[1].Value);
            foreach (Match m in PostQueued.Matches(source))
                posts.Add(m.Groups[1].Value);

            return (gets, posts);
        }

        [Fact]
        public void Spec_LoadsAndIsNonEmpty()
        {
            var spec = LoadSpec();
            var ops = SpecOperations(spec).ToList();
            Assert.True(ops.Count > 30,
                $"openapi.yaml only declares {ops.Count} operations; expected >30. " +
                "Either spec is truncated or the loader is broken.");
        }

        [Fact]
        public void Spec_OperationIdsAreUnique()
        {
            var spec = LoadSpec();
            var ids = SpecOperations(spec).Select(o => o.OperationId).ToList();
            var dupes = ids.GroupBy(x => x).Where(g => g.Count() > 1).Select(g => g.Key).ToList();
            Assert.Empty(dupes);
        }

        [Fact]
        public void Spec_EveryOperationHasAnOperationId()
        {
            var spec = LoadSpec();
            var missing = SpecOperations(spec)
                .Where(o => string.IsNullOrEmpty(o.OperationId))
                .Select(o => $"{o.Method.ToUpper()} {o.Path}")
                .ToList();
            Assert.Empty(missing);
        }

        [Fact]
        public void EveryGetRouteInCode_IsDeclaredInSpec()
        {
            var spec = LoadSpec();
            var specGets = new HashSet<string>(
                SpecOperations(spec).Where(o => o.Method == "get").Select(o => o.Path),
                StringComparer.Ordinal);

            var (sourceGets, _) = SourceRoutes(LoadHttpServerSource());
            var missing = sourceGets.Except(specGets).OrderBy(x => x).ToList();
            Assert.True(missing.Count == 0,
                "GET routes in TimberbotHttpServer.cs are missing from openapi.yaml:\n  " +
                string.Join("\n  ", missing));
        }

        [Fact]
        public void EveryPostRouteInCode_IsDeclaredInSpec()
        {
            var spec = LoadSpec();
            var specPosts = new HashSet<string>(
                SpecOperations(spec).Where(o => o.Method == "post").Select(o => o.Path),
                StringComparer.Ordinal);

            var (_, sourcePosts) = SourceRoutes(LoadHttpServerSource());
            var missing = sourcePosts.Except(specPosts).OrderBy(x => x).ToList();
            Assert.True(missing.Count == 0,
                "POST routes in TimberbotHttpServer.cs are missing from openapi.yaml:\n  " +
                string.Join("\n  ", missing));
        }

        [Fact]
        public void EveryGetSpecRoute_IsImplementedInCode()
        {
            var spec = LoadSpec();
            var specGets = SpecOperations(spec).Where(o => o.Method == "get").Select(o => o.Path).ToList();

            var (sourceGets, _) = SourceRoutes(LoadHttpServerSource());
            var phantom = specGets.Except(sourceGets).OrderBy(x => x).ToList();
            Assert.True(phantom.Count == 0,
                "GET routes documented in openapi.yaml have no implementation in TimberbotHttpServer.cs:\n  " +
                string.Join("\n  ", phantom));
        }

        [Fact]
        public void EveryPostSpecRoute_IsImplementedInCode()
        {
            var spec = LoadSpec();
            var specPosts = SpecOperations(spec).Where(o => o.Method == "post").Select(o => o.Path).ToList();

            var (_, sourcePosts) = SourceRoutes(LoadHttpServerSource());
            var phantom = specPosts.Except(sourcePosts).OrderBy(x => x).ToList();
            Assert.True(phantom.Count == 0,
                "POST routes documented in openapi.yaml have no implementation in TimberbotHttpServer.cs:\n  " +
                string.Join("\n  ", phantom));
        }

        // For each POST route's `Queued("/api/...", req => ...)` lambda body in
        // the C# source, collect the `req.Body?.Value<T>("field")` extractions
        // and assert every field is declared in the spec's requestBody.
        // Special cases: `/api/debug` is intentionally a free-form passthrough
        // (additionalProperties: true) so we skip the field-by-field check.
        [Fact]
        public void EveryPostBodyFieldInCode_IsDeclaredInSpec()
        {
            var spec = LoadSpec();
            var source = LoadHttpServerSource();
            var specPosts = SpecOperations(spec).Where(o => o.Method == "post").ToList();

            var freeForm = new HashSet<string>(StringComparer.Ordinal) { "/api/debug", "/api/benchmark" };

            var failures = new List<string>();
            foreach (var op in specPosts)
            {
                if (freeForm.Contains(op.Path)) continue;

                var lambda = ExtractQueuedLambda(source, op.Path);
                if (lambda == null) continue;  // covered by the route-coverage test

                var fields = new HashSet<string>(StringComparer.Ordinal);
                foreach (Match m in BodyFieldValue.Matches(lambda))
                    fields.Add(m.Groups[1].Value);
                foreach (Match m in BodyFieldIndex.Matches(lambda))
                    fields.Add(m.Groups[1].Value);

                var declared = SpecBodyFields(op.Operation);
                var undocumented = fields.Except(declared).OrderBy(x => x).ToList();
                if (undocumented.Count > 0)
                    failures.Add($"POST {op.Path} extracts {{{string.Join(", ", undocumented)}}} from req.Body but openapi.yaml does not declare {(undocumented.Count == 1 ? "it" : "them")}.");
            }

            Assert.True(failures.Count == 0,
                "Request-body schema drift:\n" + string.Join("\n", failures));
        }

        // Pull out the lambda body for a specific Queued("/api/path", req => ...).
        // Returns null if the route isn't present (caller treats as "not our problem").
        private static string? ExtractQueuedLambda(string source, string route)
        {
            var marker = "Queued(\"" + route + "\"";
            int start = source.IndexOf(marker, StringComparison.Ordinal);
            if (start < 0) return null;

            // Heuristic: scan forward for the matching paren that closes Queued(...).
            int depth = 0;
            int i = source.IndexOf('(', start);
            if (i < 0) return null;
            int begin = i;
            for (; i < source.Length; i++)
            {
                if (source[i] == '(') depth++;
                else if (source[i] == ')')
                {
                    depth--;
                    if (depth == 0) return source.Substring(begin, i - begin + 1);
                }
            }
            return null;
        }

        private static HashSet<string> SpecBodyFields(YamlMappingNode operation)
        {
            var fields = new HashSet<string>(StringComparer.Ordinal);
            if (!operation.Children.TryGetValue(new YamlScalarNode("requestBody"), out var rb)) return fields;
            if (rb is not YamlMappingNode rbm) return fields;
            if (!rbm.Children.TryGetValue(new YamlScalarNode("content"), out var content)) return fields;
            if (content is not YamlMappingNode contentMap) return fields;
            if (!contentMap.Children.TryGetValue(new YamlScalarNode("application/json"), out var media)) return fields;
            if (media is not YamlMappingNode mediaMap) return fields;
            if (!mediaMap.Children.TryGetValue(new YamlScalarNode("schema"), out var schema)) return fields;
            if (schema is not YamlMappingNode schemaMap) return fields;
            if (!schemaMap.Children.TryGetValue(new YamlScalarNode("properties"), out var props)) return fields;
            if (props is not YamlMappingNode propsMap) return fields;
            foreach (var kvp in propsMap.Children)
                fields.Add(((YamlScalarNode)kvp.Key).Value!);
            return fields;
        }

        [Fact]
        public void OpenApiVersion_MatchesPureConstant()
        {
            var spec = LoadSpec();
            var info = (YamlMappingNode)spec.Children[new YamlScalarNode("info")];
            var specVersion = ((YamlScalarNode)info.Children[new YamlScalarNode("version")]).Value!;
            Assert.Equal(TimberbotPure.OPENAPI_VERSION, specVersion);
        }
    }
}
