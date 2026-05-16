// TimberbotWebSocketServerTests.cs — in-process WS round-trip tests.
//
// The .NET ClientWebSocket + HttpListener AcceptWebSocketAsync stack runs
// without Unity dependencies, so we instantiate a real
// TimberbotWebSocketServer, dial it from the test, and assert on the wire
// frames it produces / consumes. Each test picks an unused TCP port so the
// suite is safe to run in parallel.

using System;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using Xunit;

namespace Timberbot.Tests
{
    public class TimberbotWebSocketServerTests
    {
        // Pick a free port by binding+releasing a TcpListener. Avoids port
        // collisions in parallel test runs.
        private static int FreePort()
        {
            var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
            listener.Start();
            int port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
            listener.Stop();
            return port;
        }

        private static async Task<ClientWebSocket> ConnectAsync(int port, string token = null, bool useQueryToken = false)
        {
            var ws = new ClientWebSocket();
            if (!string.IsNullOrEmpty(token) && !useQueryToken)
                ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
            var uri = useQueryToken && !string.IsNullOrEmpty(token)
                ? new Uri($"ws://127.0.0.1:{port}/api/ws?token={Uri.EscapeDataString(token)}")
                : new Uri($"ws://127.0.0.1:{port}/api/ws");
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await ws.ConnectAsync(uri, cts.Token).ConfigureAwait(false);
            return ws;
        }

        private static async Task<string> ReceiveAsync(ClientWebSocket ws, TimeSpan? timeout = null)
        {
            var buf = new byte[16 * 1024];
            var sb = new StringBuilder();
            using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(5));
            while (true)
            {
                var result = await ws.ReceiveAsync(new ArraySegment<byte>(buf), cts.Token).ConfigureAwait(false);
                sb.Append(Encoding.UTF8.GetString(buf, 0, result.Count));
                if (result.EndOfMessage) break;
            }
            return sb.ToString();
        }

        private static async Task SendAsync(ClientWebSocket ws, string body)
        {
            var bytes = Encoding.UTF8.GetBytes(body);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, cts.Token).ConfigureAwait(false);
        }

        // --- handshake ---------------------------------------------------

        [Fact]
        public async Task Handshake_SendsInitialStateSnapshot()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            state.SetGoal("two settlements");
            using var server = new TimberbotWebSocketServer(port, state);
            using var client = await ConnectAsync(port).ConfigureAwait(false);

            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            var obj = JObject.Parse(raw);
            Assert.Equal("state", obj.Value<string>("type"));
            var payload = (JObject)obj["payload"];
            Assert.Equal("two settlements", payload.Value<string>("goal"));
        }

        // --- auth --------------------------------------------------------

        [Fact]
        public async Task Auth_RejectsMissingBearer()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state, authToken: "s3cret");

            var ws = new ClientWebSocket();
            await Assert.ThrowsAnyAsync<WebSocketException>(async () =>
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/api/ws"), cts.Token).ConfigureAwait(false);
            }).ConfigureAwait(false);
        }

        [Fact]
        public async Task Auth_AcceptsValidBearer()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state, authToken: "s3cret");
            using var client = await ConnectAsync(port, "s3cret").ConfigureAwait(false);
            // Reaching here means the upgrade succeeded.
            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            Assert.Equal("state", JObject.Parse(raw).Value<string>("type"));
        }

        [Fact]
        public async Task Auth_AcceptsQueryTokenFallback()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state, authToken: "s3cret");
            using var client = await ConnectAsync(port, "s3cret", useQueryToken: true).ConfigureAwait(false);
            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            Assert.Equal("state", JObject.Parse(raw).Value<string>("type"));
        }

        // --- state broadcast on mutation --------------------------------

        [Fact]
        public async Task StateBroadcast_OnSetReady()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state);
            using var client = await ConnectAsync(port).ConfigureAwait(false);

            // Initial snapshot.
            await ReceiveAsync(client).ConfigureAwait(false);

            state.SetReady(true);

            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            var obj = JObject.Parse(raw);
            Assert.Equal("state", obj.Value<string>("type"));
            Assert.True(((JObject)obj["payload"]).Value<bool>("ready"));
        }

        // --- event broadcast on PushEvent -------------------------------

        [Fact]
        public async Task EventBroadcast_OnPushEvent()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state);
            using var client = await ConnectAsync(port).ConfigureAwait(false);
            await ReceiveAsync(client).ConfigureAwait(false);  // initial state

            server.PushEvent("day.start", 7, 1700000000L, "{\"day\":7}");

            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            var obj = JObject.Parse(raw);
            Assert.Equal("event", obj.Value<string>("type"));
            var payload = (JObject)obj["payload"];
            Assert.Equal("day.start", payload.Value<string>("event"));
            Assert.Equal(7, payload.Value<int>("day"));
            var data = (JObject)payload["data"];
            Assert.Equal(7, data.Value<int>("day"));
        }

        // --- inbound heartbeat -------------------------------------------

        [Fact]
        public async Task InboundHeartbeat_UpdatesAgentState()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            int pendingId = state.EnqueueRequest("hello");
            using var server = new TimberbotWebSocketServer(port, state);
            using var client = await ConnectAsync(port).ConfigureAwait(false);
            await ReceiveAsync(client).ConfigureAwait(false);  // initial state

            await SendAsync(client,
                $"{{\"type\":\"heartbeat\",\"payload\":{{\"agent_status\":\"running\",\"acked_request_id\":{pendingId}}}}}")
                .ConfigureAwait(false);

            // Heartbeat causes Changed → broadcast.
            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            var obj = JObject.Parse(raw);
            Assert.Equal("state", obj.Value<string>("type"));
            Assert.Equal("running", state.AgentStatus);
            Assert.Null(state.PendingRequest);
        }

        // --- ping/pong ---------------------------------------------------

        [Fact]
        public async Task Ping_RepliesWithPong()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state);
            using var client = await ConnectAsync(port).ConfigureAwait(false);
            await ReceiveAsync(client).ConfigureAwait(false);  // initial state

            await SendAsync(client, "{\"type\":\"ping\"}").ConfigureAwait(false);
            var raw = await ReceiveAsync(client).ConfigureAwait(false);
            Assert.Equal("pong", JObject.Parse(raw).Value<string>("type"));
        }

        // --- reconnect ---------------------------------------------------

        [Fact]
        public async Task Reconnect_FreshSnapshotOnSecondConnection()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            using var server = new TimberbotWebSocketServer(port, state);

            using (var first = await ConnectAsync(port).ConfigureAwait(false))
            {
                await ReceiveAsync(first).ConfigureAwait(false);  // initial state
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
                await first.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", cts.Token).ConfigureAwait(false);
            }

            state.SetReady(true);

            using var second = await ConnectAsync(port).ConfigureAwait(false);
            var raw = await ReceiveAsync(second).ConfigureAwait(false);
            var obj = JObject.Parse(raw);
            Assert.Equal("state", obj.Value<string>("type"));
            Assert.True(((JObject)obj["payload"]).Value<bool>("ready"));
        }

        // --- slow-consumer drop -----------------------------------------

        [Fact]
        public async Task SlowConsumer_DroppedAfterQueueOverflow()
        {
            int port = FreePort();
            var state = new TimberbotAgentState();
            // Long goal forces a fat payload (~64 KB) so each frame swallows
            // a noticeable chunk of the TCP send buffer; combined with a
            // tiny per-connection queue this makes the slow-consumer trip
            // deterministic on a loopback socket.
            state.SetGoal(new string('x', 64 * 1024));
            // maxSendQueue=1 so even a single un-drained frame already blocks
            // further enqueues.
            using var server = new TimberbotWebSocketServer(port, state, maxSendQueue: 1);

            using var client = await ConnectAsync(port).ConfigureAwait(false);

            // Wait until the connection is registered.
            for (int i = 0; i < 50 && server.ConnectionCount == 0; i++)
                await Task.Delay(20).ConfigureAwait(false);
            Assert.Equal(1, server.ConnectionCount);

            // Spam state mutations. The client never reads, so the kernel
            // TCP send buffer fills, SendAsync inside the sender task stalls,
            // and TryEnqueue starts returning false → BroadcastFrame drops us.
            for (int i = 0; i < 5000 && server.ConnectionCount > 0; i++)
            {
                state.SetReady(i % 2 == 0);
            }

            // Allow the dispatcher one more tick to settle if it just
            // observed the failed TryEnqueue.
            for (int i = 0; i < 50 && server.ConnectionCount > 0; i++)
                await Task.Delay(20).ConfigureAwait(false);
            Assert.Equal(0, server.ConnectionCount);
        }
    }
}
