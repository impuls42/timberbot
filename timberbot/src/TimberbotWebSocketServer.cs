// TimberbotWebSocketServer.cs — long-lived WebSocket fan-out for state +
// game events. Replaces the polling HTTP heartbeat + outbound HTTP webhook
// fan-out from the v0.9 architecture rework.
//
// Threading model:
//   - A single HttpListener runs on `wsPort` (default 8086) on its own
//     background accept thread. Every accepted connection is upgraded via
//     HttpListenerContext.AcceptWebSocketAsync() and tracked in a
//     ConcurrentDictionary<Guid, WebSocketConnection>.
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
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace Timberbot
{
    public class TimberbotWebSocketServer : IDisposable
    {
        private readonly HttpListener _listener;
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
            _listener = new HttpListener();

            var addr = string.IsNullOrWhiteSpace(listenAddress) ? "127.0.0.1" : listenAddress.Trim();
            bool wantWildcard = addr == "+" || addr == "0.0.0.0" || addr == "*";
            if (wantWildcard)
            {
                try
                {
                    _listener.Prefixes.Add($"http://+:{port}/");
                    _listener.Start();
                    TimberbotLog.Info($"ws listening on +:{port} (all interfaces)");
                }
                catch (HttpListenerException)
                {
                    TimberbotLog.Info($"ws port +:{port} failed, falling back to localhost");
                    _listener = new HttpListener();
                    _listener.Prefixes.Add($"http://localhost:{port}/");
                    _listener.Start();
                }
            }
            else
            {
                _listener.Prefixes.Add($"http://{addr}:{port}/");
                _listener.Start();
                TimberbotLog.Info($"ws listening on {addr}:{port}");
            }

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
                HttpListenerContext ctx;
                try { ctx = _listener.GetContext(); }
                catch { if (!_running) break; continue; }

                // We accept WS connections on a Task so the accept thread
                // immediately returns to GetContext().
                _ = HandleConnectionAsync(ctx);
            }
        }

        private async Task HandleConnectionAsync(HttpListenerContext ctx)
        {
            var path = ctx.Request.Url?.AbsolutePath?.TrimEnd('/')?.ToLowerInvariant() ?? "";
            if (path != "/api/ws")
            {
                Respond(ctx, 404, "{\"error\":\"not_found\"}");
                return;
            }
            if (!ctx.Request.IsWebSocketRequest)
            {
                Respond(ctx, 426, "{\"error\":\"upgrade_required\"}");
                return;
            }
            // Auth: header first, then ?token= fallback for browsers.
            if (!string.IsNullOrEmpty(_authToken))
            {
                var authHeader = ctx.Request.Headers["Authorization"];
                var presented = TimberbotPure.ExtractBearerToken(authHeader);
                if (string.IsNullOrEmpty(presented))
                    presented = ctx.Request.QueryString["token"];
                if (string.IsNullOrEmpty(presented) || !TimberbotPure.BearerTokenMatches(_authToken, presented))
                {
                    ctx.Response.Headers["WWW-Authenticate"] = "Bearer realm=\"timberbot\"";
                    Respond(ctx, 401, "{\"error\":\"unauthorized\"}");
                    return;
                }
            }

            WebSocketContext wsCtx;
            try { wsCtx = await ctx.AcceptWebSocketAsync(subProtocol: null).ConfigureAwait(false); }
            catch (Exception ex)
            {
                TimberbotLog.Error("ws.accept", ex);
                try { ctx.Response.Abort(); } catch { }
                return;
            }

            var conn = new WebSocketConnection(wsCtx.WebSocket, _maxSendQueue);
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

        private async Task ReceiveLoop(WebSocketConnection conn)
        {
            var buffer = new byte[16 * 1024];
            var sb = new StringBuilder();
            while (conn.IsOpen && _running)
            {
                WebSocketReceiveResult result;
                sb.Clear();
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
                    sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                    if (result.EndOfMessage) break;
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

        private static void Respond(HttpListenerContext ctx, int status, string body)
        {
            try
            {
                ctx.Response.StatusCode = status;
                ctx.Response.ContentType = "application/json";
                using var sw = new StreamWriter(ctx.Response.OutputStream, new UTF8Encoding(false));
                sw.Write(body);
                ctx.Response.OutputStream.Close();
            }
            catch { /* client likely hung up; ignore */ }
        }

        // Per-connection state. Owns its own bounded send queue; the sender
        // task drains the queue and writes frames to the socket sequentially.
        public sealed class WebSocketConnection
        {
            public readonly Guid Id = Guid.NewGuid();
            public readonly WebSocket Socket;
            private readonly int _capacity;
            private readonly object _queueLock = new object();
            private readonly Queue<string> _queue = new Queue<string>();
            private readonly SemaphoreSlim _signal = new SemaphoreSlim(0);
            private volatile bool _closed;

            public bool IsOpen => !_closed && Socket.State == WebSocketState.Open;

            public WebSocketConnection(WebSocket socket, int capacity)
            {
                Socket = socket;
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

            public void Close(string reason)
            {
                _closed = true;
                try { _signal.Release(); } catch { }
                // Fire-and-forget the WebSocket close handshake — never block
                // the broadcaster on a misbehaving consumer. The sender task
                // observes _closed on its next iteration and exits; Dispose
                // tears down the underlying socket once the handshake either
                // completes or its 5s timeout elapses.
                if (Socket.State == WebSocketState.Open)
                {
                    var sock = Socket;
                    var label = reason ?? "closed";
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
                        }
                    });
                }
                else
                {
                    try { Socket.Dispose(); } catch { }
                }
            }
        }
    }
}
