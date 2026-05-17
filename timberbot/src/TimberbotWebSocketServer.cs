// TimberbotWebSocketServer.cs — long-lived WebSocket fan-out for state +
// game events. Replaces the polling HTTP heartbeat + outbound HTTP webhook
// fan-out from the v0.9 architecture rework.
//
// Threading model:
//   - A single TcpListener runs on `wsPort` (default 8086) on its own
//     background accept thread. Every accepted connection has its HTTP
//     upgrade request parsed manually, then `WebSocket.CreateFromStream`
//     wraps the live NetworkStream and the connection is tracked in a
//     ConcurrentDictionary<Guid, WebSocketConnection>.
//   - Why not HttpListener.AcceptWebSocketAsync? Mono's HttpListener (the
//     Unity runtime) throws `NotImplementedException` from that method.
//     We do the RFC 6455 handshake by hand: parse Connection / Upgrade /
//     Sec-WebSocket-{Key,Version}, compute Sec-WebSocket-Accept, write a
//     101 Switching Protocols response, then hand off the stream.
//   - Each connection runs its own receive loop (`Task.Run`) reading
//     client->server frames (`heartbeat`, `ping`) until the socket closes.
//   - Each connection has a bounded send queue. PushState / PushEvent enqueue
//     a frame and a per-connection dispatcher drains it asynchronously. When
//     the queue overflows (slow consumer), the connection is dropped instead
//     of stalling the fan-out — broadcasts MUST never back-pressure
//     game-event handlers.
//   - All TimberbotAgentState mutations go through the existing write-job
//     queue (HTTP layer) or directly off the listener thread; the WS server
//     never touches game state, it just relays state-snapshot JSON.
//
// Bearer auth: when `authToken` is set, the upgrade request must carry
// `Authorization: Bearer <token>`. Browsers that can't set headers on a WS
// upgrade can fall back to `?token=<value>` in the query string. Either form
// must match the configured `authToken` via constant-time compare, or the
// upgrade is refused with HTTP 401 before WebSocket negotiation happens.
//
// Protocol surface is documented authoritatively in `docs/websocket-protocol.md`.

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace Timberbot
{
    public class TimberbotWebSocketServer : IDisposable
    {
        private readonly TcpListener _listener;
        private readonly Thread _acceptThread;
        private readonly string _authToken;
        private readonly CancellationTokenSource _shutdown = new CancellationTokenSource();
        private readonly ConcurrentDictionary<Guid, WebSocketConnection> _connections =
            new ConcurrentDictionary<Guid, WebSocketConnection>();
        private readonly TimberbotAgentState _agentState;
        private volatile bool _running;
        private readonly int _maxSendQueue;

        // Default per-connection bounded send queue. ~256 frames is plenty for
        // the 30s heartbeat cadence + game-event spikes; anything beyond that
        // is a slow consumer we'd rather drop than stall the broadcaster on.
        public const int DefaultMaxSendQueue = 256;

        // Cap inbound headers so a misbehaving client can't grow the parse
        // buffer unboundedly during the handshake. 8 KB covers any reasonable
        // browser/aiohttp set.
        private const int MaxHeaderBytes = 8 * 1024;

        // Public for callers that need to publish without taking a reference
        // to the listener thread (e.g. webhook handlers).
        public int ConnectionCount => _connections.Count;

        public TimberbotWebSocketServer(
            int port,
            TimberbotAgentState agentState,
            string listenAddress = "127.0.0.1",
            string authToken = "",
            int maxSendQueue = DefaultMaxSendQueue)
        {
            _agentState = agentState;
            _authToken = TimberbotPure.NormalizeAuthToken(authToken);
            _maxSendQueue = maxSendQueue > 0 ? maxSendQueue : DefaultMaxSendQueue;

            var addr = string.IsNullOrWhiteSpace(listenAddress) ? "127.0.0.1" : listenAddress.Trim();
            IPAddress ip;
            if (addr == "+" || addr == "0.0.0.0" || addr == "*")
                ip = IPAddress.Any;
            else if (string.Equals(addr, "localhost", StringComparison.OrdinalIgnoreCase))
                ip = IPAddress.Loopback;
            else if (!IPAddress.TryParse(addr, out ip))
                ip = IPAddress.Loopback;

            _listener = new TcpListener(ip, port);
            _listener.Start();
            TimberbotLog.Info($"ws listening on {ip}:{port}");

            _running = true;
            if (_agentState != null)
                _agentState.Changed += OnAgentStateChanged;

            _acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "Timberbot-WS" };
            _acceptThread.Start();
        }

        public void Stop()
        {
            _running = false;
            try { _shutdown.Cancel(); } catch { }
            if (_agentState != null)
                _agentState.Changed -= OnAgentStateChanged;
            foreach (var kvp in _connections)
            {
                try { kvp.Value.Close("server_shutdown"); } catch { }
            }
            _connections.Clear();
            try { _listener.Stop(); } catch { }
        }

        public void Dispose() => Stop();

        // Public hook for game-event publishers.
        public void PushEvent(string eventName, int day, long timestampUnix, string dataJson)
        {
            var frame = TimberbotPure.BuildEventMessage(eventName, day, timestampUnix, dataJson);
            BroadcastFrame(frame);
        }

        // Game state pushed in response to AgentState.Changed.
        private void OnAgentStateChanged(string stateJson)
        {
            var frame = TimberbotPure.BuildStateMessage(stateJson);
            BroadcastFrame(frame);
        }

        private void BroadcastFrame(string frame)
        {
            foreach (var kvp in _connections)
            {
                var conn = kvp.Value;
                if (!conn.TryEnqueue(frame))
                {
                    TimberbotLog.Info($"ws.drop slow consumer id={conn.Id}");
                    try { conn.Close("slow_consumer"); } catch { }
                    _connections.TryRemove(conn.Id, out _);
                }
            }
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client;
                try { client = _listener.AcceptTcpClient(); }
                catch { if (!_running) break; continue; }

                // Handshake + per-connection loop runs on a Task so the
                // accept thread immediately returns to AcceptTcpClient().
                _ = HandleConnectionAsync(client);
            }
        }

        private async Task HandleConnectionAsync(TcpClient client)
        {
            NetworkStream stream;
            try { stream = client.GetStream(); }
            catch (Exception ex)
            {
                TimberbotLog.Info($"ws.accept.stream.err {ex.GetType().Name}:{ex.Message}");
                try { client.Close(); } catch { }
                return;
            }

            // Read the HTTP upgrade request. We read until \r\n\r\n with a
            // hard cap so a misbehaving client can't allocate unboundedly.
            string requestText;
            try { requestText = await ReadHttpHeadersAsync(stream, _shutdown.Token).ConfigureAwait(false); }
            catch (Exception ex)
            {
                TimberbotLog.Info($"ws.handshake.read.err {ex.GetType().Name}:{ex.Message}");
                try { client.Close(); } catch { }
                return;
            }
            if (requestText == null)
            {
                await WriteHttpResponseAsync(stream, 400, "{\"error\":\"malformed_request\"}").ConfigureAwait(false);
                try { client.Close(); } catch { }
                return;
            }

            var (method, target, headers) = ParseHttpRequest(requestText);
            if (method != "GET")
            {
                await WriteHttpResponseAsync(stream, 405, "{\"error\":\"method_not_allowed\"}").ConfigureAwait(false);
                try { client.Close(); } catch { }
                return;
            }

            // Path + query split. We only serve `/api/ws`; the rest 404s.
            string path = target;
            string queryString = "";
            var q = target.IndexOf('?');
            if (q >= 0) { path = target.Substring(0, q); queryString = target.Substring(q + 1); }
            path = (path ?? "").TrimEnd('/').ToLowerInvariant();
            if (path != "/api/ws")
            {
                await WriteHttpResponseAsync(stream, 404, "{\"error\":\"not_found\"}").ConfigureAwait(false);
                try { client.Close(); } catch { }
                return;
            }

            headers.TryGetValue("connection", out var connHeader);
            headers.TryGetValue("upgrade", out var upgradeHeader);
            headers.TryGetValue("sec-websocket-key", out var wsKey);
            headers.TryGetValue("sec-websocket-version", out var wsVersion);
            if (!TimberbotPure.IsWebSocketUpgradeRequest(connHeader, upgradeHeader, wsKey, wsVersion))
            {
                await WriteHttpResponseAsync(stream, 426, "{\"error\":\"upgrade_required\"}").ConfigureAwait(false);
                try { client.Close(); } catch { }
                return;
            }

            // Auth: Authorization: Bearer header first, then ?token= fallback.
            if (!string.IsNullOrEmpty(_authToken))
            {
                headers.TryGetValue("authorization", out var authHeader);
                var presented = TimberbotPure.ExtractBearerToken(authHeader);
                if (string.IsNullOrEmpty(presented))
                    presented = ParseQueryParam(queryString, "token");
                if (string.IsNullOrEmpty(presented) || !TimberbotPure.BearerTokenMatches(_authToken, presented))
                {
                    await WriteHttpResponseAsync(stream, 401, "{\"error\":\"unauthorized\"}",
                        extraHeaders: "WWW-Authenticate: Bearer realm=\"timberbot\"\r\n").ConfigureAwait(false);
                    try { client.Close(); } catch { }
                    return;
                }
            }

            // Write 101 Switching Protocols + handshake response.
            var accept = TimberbotPure.ComputeWebSocketAccept(wsKey);
            var resp = "HTTP/1.1 101 Switching Protocols\r\n" +
                       "Upgrade: websocket\r\n" +
                       "Connection: Upgrade\r\n" +
                       $"Sec-WebSocket-Accept: {accept}\r\n" +
                       "\r\n";
            try
            {
                var respBytes = Encoding.ASCII.GetBytes(resp);
                await stream.WriteAsync(respBytes, 0, respBytes.Length).ConfigureAwait(false);
                await stream.FlushAsync().ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                TimberbotLog.Info($"ws.handshake.write.err {ex.GetType().Name}:{ex.Message}");
                try { client.Close(); } catch { }
                return;
            }

            WebSocket ws;
            try
            {
                ws = WebSocket.CreateFromStream(stream, isServer: true, subProtocol: null,
                    keepAliveInterval: TimeSpan.FromSeconds(30));
            }
            catch (Exception ex)
            {
                TimberbotLog.Error("ws.create_from_stream", ex);
                try { client.Close(); } catch { }
                return;
            }

            var conn = new WebSocketConnection(ws, _maxSendQueue, client);
            _connections[conn.Id] = conn;
            TimberbotLog.Info($"ws.connect id={conn.Id} count={_connections.Count}");

            // Push initial snapshot so freshly-connected clients don't have to
            // wait for the next mutation to learn the current state.
            if (_agentState != null)
            {
                var initial = TimberbotPure.BuildStateMessage(_agentState.ToStateResponseJson());
                conn.TryEnqueue(initial);
            }

            var senderTask = conn.RunSenderAsync(_shutdown.Token);
            try
            {
                await ReceiveLoop(conn).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                TimberbotLog.Info($"ws.recv.err id={conn.Id} ex={ex.GetType().Name}:{ex.Message}");
            }
            finally
            {
                _connections.TryRemove(conn.Id, out _);
                try { conn.Close("client_closed"); } catch { }
                try { await senderTask.ConfigureAwait(false); } catch { }
                TimberbotLog.Info($"ws.disconnect id={conn.Id} count={_connections.Count}");
            }
        }

        // Cap inbound WS frames so a misbehaving client that never sends
        // EndOfMessage can't grow the reassembly buffer unboundedly. 64 KB
        // is roughly two orders of magnitude over the largest legitimate
        // heartbeat payload.
        public const int MaxInboundMessageBytes = 64 * 1024;

        private async Task ReceiveLoop(WebSocketConnection conn)
        {
            var buffer = new byte[16 * 1024];
            var sb = new StringBuilder();
            while (conn.IsOpen && _running)
            {
                WebSocketReceiveResult result;
                sb.Clear();
                int totalBytes = 0;
                bool overflow = false;
                while (true)
                {
                    try
                    {
                        result = await conn.Socket.ReceiveAsync(
                            new ArraySegment<byte>(buffer), _shutdown.Token).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException) { return; }
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        try
                        {
                            await conn.Socket.CloseAsync(
                                WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None)
                                .ConfigureAwait(false);
                        }
                        catch { }
                        return;
                    }
                    totalBytes += result.Count;
                    if (totalBytes > MaxInboundMessageBytes)
                    {
                        overflow = true;
                        // Keep draining the rest of this frame so the client
                        // doesn't get stuck mid-send, but discard the bytes.
                        if (result.EndOfMessage) break;
                        continue;
                    }
                    sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                    if (result.EndOfMessage) break;
                }
                if (overflow)
                {
                    conn.TryEnqueue(TimberbotPure.BuildErrorMessage(
                        $"message_too_large: cap {MaxInboundMessageBytes} bytes"));
                    continue;
                }
                var raw = sb.ToString();
                var msg = TimberbotPure.ParseInboundMessage(raw);
                if (msg == null)
                {
                    conn.TryEnqueue(TimberbotPure.BuildErrorMessage("invalid_message"));
                    continue;
                }
                switch (msg.Type)
                {
                    case TimberbotPure.WsTypeHeartbeat:
                        if (_agentState != null)
                        {
                            _agentState.Heartbeat(msg.AgentStatus, msg.AckedRequestId, DateTime.UtcNow);
                        }
                        break;
                    case TimberbotPure.WsTypePing:
                        conn.TryEnqueue(TimberbotPure.BuildPongMessage());
                        break;
                    default:
                        conn.TryEnqueue(TimberbotPure.BuildErrorMessage("unknown_type: " + msg.Type));
                        break;
                }
            }
        }

        // --- handshake helpers (manual HTTP/1.1, by-hand because Mono's
        //     HttpListener doesn't implement AcceptWebSocketAsync) ---

        // Read until \r\n\r\n with a hard cap. Returns the full request bytes
        // as ASCII text, or null on malformed input. We deliberately do NOT
        // consume past the header delimiter — RFC 6455 mandates an empty body
        // on the upgrade GET, and WebSocket framing starts on the next byte
        // the client sends after seeing 101 Switching Protocols.
        private static async Task<string> ReadHttpHeadersAsync(NetworkStream stream, CancellationToken ct)
        {
            var buf = new byte[MaxHeaderBytes];
            int total = 0;
            while (total < MaxHeaderBytes)
            {
                int n;
                try { n = await stream.ReadAsync(buf, total, 1, ct).ConfigureAwait(false); }
                catch { return null; }
                if (n == 0) return null;
                total += n;
                if (total >= 4 && buf[total - 4] == (byte)'\r' && buf[total - 3] == (byte)'\n'
                    && buf[total - 2] == (byte)'\r' && buf[total - 1] == (byte)'\n')
                {
                    return Encoding.ASCII.GetString(buf, 0, total - 4);
                }
            }
            return null;  // hit MaxHeaderBytes without seeing the delimiter
        }

        private static async Task WriteHttpResponseAsync(NetworkStream stream, int status, string body,
            string extraHeaders = "")
        {
            try
            {
                string reason = status switch
                {
                    400 => "Bad Request",
                    401 => "Unauthorized",
                    404 => "Not Found",
                    405 => "Method Not Allowed",
                    426 => "Upgrade Required",
                    _ => "Error",
                };
                var bodyBytes = Encoding.UTF8.GetBytes(body ?? "");
                var head = $"HTTP/1.1 {status} {reason}\r\n" +
                           "Content-Type: application/json\r\n" +
                           $"Content-Length: {bodyBytes.Length}\r\n" +
                           "Connection: close\r\n" +
                           (extraHeaders ?? "") +
                           "\r\n";
                var headBytes = Encoding.ASCII.GetBytes(head);
                await stream.WriteAsync(headBytes, 0, headBytes.Length).ConfigureAwait(false);
                if (bodyBytes.Length > 0)
                    await stream.WriteAsync(bodyBytes, 0, bodyBytes.Length).ConfigureAwait(false);
                await stream.FlushAsync().ConfigureAwait(false);
            }
            catch { /* client likely hung up */ }
        }

        // Parses an HTTP/1.1 request from its header text. Returns the method,
        // request-target (path + optional query), and a lowercased-key header
        // dictionary. We tolerate folded headers via simple concat — not
        // strictly RFC-compliant but matches what aiohttp / browsers emit.
        private static (string method, string target, Dictionary<string, string> headers) ParseHttpRequest(string text)
        {
            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrEmpty(text)) return ("", "", headers);
            var lines = text.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0) return ("", "", headers);
            var requestLine = lines[0];
            var parts = requestLine.Split(' ');
            string method = parts.Length > 0 ? parts[0].ToUpperInvariant() : "";
            string target = parts.Length > 1 ? parts[1] : "";
            for (int i = 1; i < lines.Length; i++)
            {
                var line = lines[i];
                var colon = line.IndexOf(':');
                if (colon <= 0) continue;
                var name = line.Substring(0, colon).Trim().ToLowerInvariant();
                var value = line.Substring(colon + 1).Trim();
                if (headers.TryGetValue(name, out var existing))
                    headers[name] = existing + ", " + value;
                else
                    headers[name] = value;
            }
            return (method, target, headers);
        }

        // Pull a single query-string parameter value (case-sensitive name).
        // Returns null when the parameter is absent. URL-decoding is minimal:
        // we treat the value as opaque (no `+` → space substitution); auth
        // tokens are typically url-safe-base64 which doesn't need decoding.
        private static string ParseQueryParam(string queryString, string name)
        {
            if (string.IsNullOrEmpty(queryString)) return null;
            foreach (var pair in queryString.Split('&'))
            {
                var eq = pair.IndexOf('=');
                string k, v;
                if (eq < 0) { k = pair; v = ""; }
                else { k = pair.Substring(0, eq); v = pair.Substring(eq + 1); }
                if (k == name) return Uri.UnescapeDataString(v);
            }
            return null;
        }

        // Per-connection state. Owns its own bounded send queue; the sender
        // task drains the queue and writes frames to the socket sequentially.
        // Also owns the TcpClient that backs the WebSocket so the TCP socket
        // is closed when the WS half is done.
        public sealed class WebSocketConnection
        {
            public readonly Guid Id = Guid.NewGuid();
            public readonly WebSocket Socket;
            private readonly TcpClient _tcp;
            private readonly int _capacity;
            private readonly object _queueLock = new object();
            private readonly Queue<string> _queue = new Queue<string>();
            private readonly SemaphoreSlim _signal = new SemaphoreSlim(0);
            private volatile bool _closed;

            public bool IsOpen => !_closed && Socket.State == WebSocketState.Open;

            public WebSocketConnection(WebSocket socket, int capacity, TcpClient tcp = null)
            {
                Socket = socket;
                _tcp = tcp;
                _capacity = capacity > 0 ? capacity : DefaultMaxSendQueue;
            }

            // Returns false when the bounded queue is full — caller is
            // responsible for dropping the connection. Closed connections also
            // return false (they will be cleaned up shortly).
            public bool TryEnqueue(string frame)
            {
                if (_closed) return false;
                lock (_queueLock)
                {
                    if (_queue.Count >= _capacity) return false;
                    _queue.Enqueue(frame);
                }
                try { _signal.Release(); } catch { }
                return true;
            }

            public async Task RunSenderAsync(CancellationToken ct)
            {
                try
                {
                    while (!_closed)
                    {
                        try { await _signal.WaitAsync(ct).ConfigureAwait(false); }
                        catch (OperationCanceledException) { return; }
                        string frame = null;
                        lock (_queueLock)
                        {
                            if (_queue.Count > 0) frame = _queue.Dequeue();
                        }
                        if (frame == null) continue;
                        if (Socket.State != WebSocketState.Open) return;
                        var bytes = Encoding.UTF8.GetBytes(frame);
                        try
                        {
                            await Socket.SendAsync(
                                new ArraySegment<byte>(bytes),
                                WebSocketMessageType.Text,
                                endOfMessage: true,
                                cancellationToken: ct).ConfigureAwait(false);
                        }
                        catch
                        {
                            return;
                        }
                    }
                }
                finally
                {
                    _closed = true;
                }
            }

            // Idempotent: called from multiple paths
            //   - BroadcastFrame when TryEnqueue overflows (slow_consumer drop)
            //   - HandleConnectionAsync's finally after the ReceiveLoop ends
            //   - TimberbotWebSocketServer.Stop on shutdown
            // The _closed flag guards the sender; the WebSocket.CloseAsync
            // call is a no-op once the socket is already closing, and the
            // Dispose() in the Task.Run continuation is safe to invoke
            // twice. Fire-and-forget so a slow handshake never back-presses
            // the broadcaster.
            public void Close(string reason)
            {
                bool wasOpen = !_closed;
                _closed = true;
                try { _signal.Release(); } catch { }
                if (!wasOpen) return;  // second caller can skip the rest

                if (Socket.State == WebSocketState.Open)
                {
                    var sock = Socket;
                    var label = reason ?? "closed";
                    var tcp = _tcp;
                    var signal = _signal;
                    Task.Run(async () =>
                    {
                        try
                        {
                            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                            await sock.CloseAsync(WebSocketCloseStatus.NormalClosure, label, cts.Token)
                                .ConfigureAwait(false);
                        }
                        catch { }
                        finally
                        {
                            try { sock.Dispose(); } catch { }
                            try { tcp?.Close(); } catch { }
                            try { signal.Dispose(); } catch { }
                        }
                    });
                }
                else
                {
                    try { Socket.Dispose(); } catch { }
                    try { _tcp?.Close(); } catch { }
                    try { _signal.Dispose(); } catch { }
                }
            }
        }
    }
}
