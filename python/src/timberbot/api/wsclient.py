"""Async WebSocket client wrapper for the Timberbot mod.

The WS channel replaces the heartbeat-polling + outbound-HTTP-webhook split
with a single long-lived bidirectional connection. `TimberbotWsClient` is the
Python side; the C# server lives in the mod (Unit 1, #28). Tests stand up an
in-process `aiohttp.web` server as a stand-in so this file doesn't depend on
the real mod at test time.

Behavior:

  - Wraps `aiohttp.ClientSession.ws_connect()`.
  - Auto-reconnects on close/error using the shared `exp_backoff(1s→30s)`
    helper in `timberbot.utils` — same backoff the HTTP connector uses, so
    the operator sees one consistent cadence.
  - Async iteration over typed inbound frames (Pydantic-parsed). Bad JSON or
    frames missing the `{type, payload}` envelope are logged and dropped so
    the iterator stays alive across malformed messages.
  - `send_message(msg_type, payload)` constructs `{type, payload}` envelopes
    and pushes them as text frames.
  - Threads `Authorization: Bearer <token>` on the upgrade request when
    `auth_token` is set. An optional `?token=` query-param fallback is
    available via `query_token_fallback=True` for environments where
    intermediaries strip custom headers (default: off). When the fallback is
    active, log lines redact the token so it doesn't leak into client logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
from aiohttp import ClientWebSocketResponse, WSMsgType
from pydantic import BaseModel, ValidationError

from timberbot.api.models.ws import INBOUND_PAYLOAD_MODELS, WsMessage
from timberbot.settings import resolve_auth_token
from timberbot.utils import exp_backoff

log = logging.getLogger("timberbot.wsclient")


class TimberbotWsClient:
    """Async WebSocket client for the Timberbot mod.

    Lifecycle:

        client = TimberbotWsClient("127.0.0.1", 8086, auth_token="…")
        await client.connect()
        async for msg in client.messages():
            ...
        await client.close()

    `connect()` opens the initial socket. Subsequent reconnects happen
    transparently inside `messages()` when the peer closes or errors —
    callers do not need to call `connect()` again.

    The class is **not** thread-safe; drive it from a single asyncio task or
    cooperatively from a task group.
    """

    def __init__(
        self,
        host: str,
        ws_port: int,
        auth_token: str | None = None,
        *,
        query_token_fallback: bool = False,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        path: str = "/ws",
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        # Same resolution chain as TimberbotClient: constructor arg →
        # TBOT_AUTH_TOKEN env → [client].auth_token in user config.
        self.host = host
        self.ws_port = ws_port
        self.auth_token = resolve_auth_token(auth_token)
        self.query_token_fallback = query_token_fallback
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.path = path if path.startswith("/") else f"/{path}"

        self._session = session
        self._owns_session = session is None
        self._ws: ClientWebSocketResponse | None = None
        self._closed = False
        self._reconnect_attempts = 0

    # ------------------------------------------------------------------
    # URL / header construction
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The `ws://host:port/path` URL the client will dial.

        Header-based bearer auth is always preferred; `query_token_fallback`
        exists only for environments where intermediaries strip custom headers.
        """
        base = f"ws://{self.host}:{self.ws_port}{self.path}"
        if self.query_token_fallback and self.auth_token:
            return f"{base}?token={self.auth_token}"
        return base

    @property
    def safe_url(self) -> str:
        """`url` with any `?token=…` query param redacted.

        The token-in-URL fallback is intentionally rare, but when it's enabled
        we still don't want the secret to land in client log files. Server
        access logs are the operator's problem to redact; this property keeps
        OUR logs clean.
        """
        if self.query_token_fallback and self.auth_token:
            return f"ws://{self.host}:{self.ws_port}{self.path}?token=***"
        return self.url

    def _headers(self) -> dict[str, str]:
        """`Authorization: Bearer <token>` when set, else empty."""
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        return {}

    # ------------------------------------------------------------------
    # Session / connect / close
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _dial(self) -> ClientWebSocketResponse:
        session = await self._ensure_session()
        return await session.ws_connect(self.url, headers=self._headers())

    async def connect(self) -> None:
        """Open the initial WS connection.

        Raises `aiohttp.WSServerHandshakeError` on auth failure (e.g. 401 when
        the token is missing or wrong) so callers can distinguish it from
        transient network errors and refuse to spin in reconnect mode.
        """
        self._closed = False
        self._ws = await self._dial()
        self._reconnect_attempts = 0
        log.info("wsclient: connected to %s", self.safe_url)

    async def close(self) -> None:
        """Close the socket and (if we own it) the underlying ClientSession.

        Idempotent — safe to call from a `finally` block even if `connect()`
        never succeeded.
        """
        self._closed = True
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                log.debug("wsclient: error during ws close", exc_info=True)
        self._ws = None
        if self._owns_session and self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001
                log.debug("wsclient: error during session close", exc_info=True)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send_message(self, msg_type: str, payload: dict[str, Any]) -> None:
        """Send a `{type, payload}` envelope as a text frame.

        `msg_type` is the envelope `type` string (e.g. `"heartbeat"`).
        Pydantic `BaseModel` payloads are accepted and serialized via
        `model_dump(mode="json")` so enum values round-trip correctly.
        Raises `RuntimeError` if the socket is not currently open — callers
        should drive `messages()` (which auto-reconnects) before sending.
        """
        if self._ws is None or self._ws.closed:
            raise RuntimeError("wsclient: not connected; call connect() first")
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        frame = json.dumps({"type": msg_type, "payload": payload})
        await self._ws.send_str(frame)

    # ------------------------------------------------------------------
    # Receive iterator (with auto-reconnect)
    # ------------------------------------------------------------------

    async def messages(self) -> AsyncIterator[WsMessage[Any]]:
        """Yield parsed `WsMessage` envelopes until `close()` is called.

        Reconnect policy: on `WSMsgType.CLOSE/CLOSED/CLOSING/ERROR`, the
        socket is replaced via a fresh `ws_connect()` after sleeping
        `exp_backoff(attempt, base, cap)`. The iterator does NOT yield a
        sentinel on disconnect — it just keeps producing the next frame from
        the new socket.

        Malformed frames (bad JSON, missing `{type, payload}`, payload that
        fails Pydantic validation) are logged and dropped; the iterator
        survives them.

        Exit contract:
          * `close()` was called — the iterator returns cleanly.
          * The handshake failed with HTTP 401 — `_reconnect` re-raises so
            the caller stops spinning on a bad token.
          * Any other condition keeps looping; transient handshake and dial
            failures are absorbed inside `_reconnect`, which keeps `_ws=None`
            and lets the next loop iteration retry with the next backoff
            step.
        """
        if self._ws is None and not self._closed:
            await self.connect()

        while not self._closed:
            ws = self._ws
            if ws is None:
                # Last reconnect attempt failed transiently (handshake or
                # dial). Sleep + retry; don't bail on `messages()` — that
                # would silently terminate the iterator and force every
                # caller to wrap us in their own outer loop.
                await self._reconnect()
                continue
            try:
                async for raw in ws:
                    parsed = self._parse_frame(raw)
                    if parsed is not None:
                        yield parsed
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("wsclient: receive loop errored (%s); reconnecting", exc)

            if self._closed:
                break

            # Peer closed (or errored) — try to reconnect.
            await self._reconnect()

    def _parse_frame(self, raw: aiohttp.WSMessage) -> WsMessage[Any] | None:
        """Parse one raw aiohttp frame; return None to drop it.

        Drop reasons (each logged at WARNING):
          * non-text frame (binary/ping/pong handled by aiohttp itself);
          * close/error frame (returned as None so the outer loop reconnects);
          * payload that isn't valid JSON;
          * payload missing the `{type, payload}` envelope;
          * payload that fails Pydantic validation for its declared `type`.
        """
        if raw.type == WSMsgType.TEXT:
            data: str = raw.data
        elif raw.type == WSMsgType.BINARY:
            try:
                data = raw.data.decode("utf-8")
            except UnicodeDecodeError:
                log.warning("wsclient: dropped non-utf8 binary frame")
                return None
        elif raw.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            return None
        elif raw.type == WSMsgType.ERROR:
            log.warning("wsclient: ws error frame: %s", raw.data)
            return None
        else:
            # ping/pong are handled by aiohttp itself; anything else we don't know.
            return None

        try:
            envelope = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            log.warning("wsclient: dropped frame with bad JSON: %r", data[:200])
            return None

        if not isinstance(envelope, dict) or "type" not in envelope:
            log.warning("wsclient: dropped frame missing {type, payload}: %r", envelope)
            return None

        msg_type = envelope.get("type")
        payload = envelope.get("payload")
        model = INBOUND_PAYLOAD_MODELS.get(msg_type) if isinstance(msg_type, str) else None
        if model is not None:
            try:
                payload = model.model_validate(payload or {})
            except ValidationError as exc:
                log.warning("wsclient: dropped frame with invalid payload for type=%s: %s",
                            msg_type, exc)
                return None
        # Unknown types pass through with the raw payload — keeps the protocol
        # forward-compatible so new server-side frame types don't require a
        # client release to be readable.
        return WsMessage(type=str(msg_type), payload=payload)

    async def _reconnect(self) -> None:
        """Close the current socket (best-effort) and dial again with backoff.

        On success, resets the attempt counter so the next disconnect starts
        from `base` again. On a hard auth failure
        (`WSServerHandshakeError` with status 401) we re-raise — there's no
        point spinning if the token is wrong.
        """
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                log.debug("wsclient: ignored error during close on reconnect", exc_info=True)
        self._ws = None

        delay = exp_backoff(self._reconnect_attempts,
                            base=self.backoff_base, cap=self.backoff_cap)
        log.info("wsclient: reconnect attempt %d, sleeping %.1fs",
                 self._reconnect_attempts, delay)
        self._reconnect_attempts += 1
        await asyncio.sleep(delay)

        if self._closed:
            return
        try:
            self._ws = await self._dial()
            self._reconnect_attempts = 0
            log.info("wsclient: reconnected to %s", self.safe_url)
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status == 401:
                # Bad token; bubbling up lets the caller stop the loop.
                self._closed = True
                raise
            log.warning("wsclient: handshake failed (%s); will retry on next tick", exc)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("wsclient: reconnect dial failed (%s); will retry on next tick", exc)
