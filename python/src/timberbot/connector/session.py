from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from enum import Enum

from timberbot.connector.protocol import (
    ACP_VERSION,
    METHOD_INITIALIZE,
    METHOD_SESSION_CANCEL,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    NOTIF_GAME_ELICITATION,
    NOTIF_SESSION_ENDED,
    NOTIF_SESSION_REQUEST_PERMISSION,
    NOTIF_SESSION_UPDATE,
    build_request,
)

log = logging.getLogger("timberbot.connector")


class SessionState(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    HALTING = "halting"
    ENDED = "ended"


class SessionHandle:
    def __init__(
        self,
        transport: object,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self._transport = transport
        self._allowed_tools: list[str] = allowed_tools or []
        self._next_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}

        self.state: SessionState = SessionState.PENDING
        self.session_id: str | None = None
        self.on_update: Callable[[str, str], Awaitable[None]] | None = None
        self.on_elicitation: Callable[[str, dict], Awaitable[None]] | None = None
        self._read_task: asyncio.Task | None = None

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        req_id = self._alloc_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._transport.send(build_request(req_id, method, params))
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._pending.pop(req_id, None)
            raise

    async def initialize(self) -> None:
        await self._request(
            METHOD_INITIALIZE,
            {"protocolVersion": ACP_VERSION, "clientCapabilities": {"fs": False, "terminal": False}},
            timeout=30.0,
        )
        self.state = SessionState.ACTIVE

    async def new_session(self, cwd: str, mcp_servers: list[dict]) -> str:
        result = await self._request(
            METHOD_SESSION_NEW,
            {"cwd": cwd, "mcpServers": mcp_servers},
        )
        self.session_id = result["sessionId"]
        return self.session_id

    async def prompt(self, session_id: str, text: str) -> None:
        await self._request(METHOD_SESSION_PROMPT, {"sessionId": session_id, "text": text})

    async def cancel(self, session_id: str) -> None:
        await self._request(METHOD_SESSION_CANCEL, {"sessionId": session_id})
        self.state = SessionState.HALTING

    async def read_loop(self) -> None:
        self._read_task = asyncio.current_task()
        while True:
            msg = await self._transport.recv_line()
            if msg is None:
                self._finalize()
                break

            if "id" in msg and "method" not in msg:
                req_id = msg["id"]
                fut = self._pending.pop(req_id, None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(msg["error"]))
                    else:
                        fut.set_result(msg.get("result", {}))
                continue

            method = msg.get("method")
            if not method:
                continue

            params = msg.get("params", {})

            if method == NOTIF_SESSION_UPDATE:
                if self.on_update:
                    sid = params.get("sessionId", "")
                    chunk = params.get("chunk", "")
                    await self.on_update(sid, chunk)

            elif method == NOTIF_SESSION_REQUEST_PERMISSION:
                req_id = msg.get("id")
                tool_name = params.get("toolName", "")
                approved = any(fnmatch.fnmatch(tool_name, pat) for pat in self._allowed_tools)
                response = {"jsonrpc": "2.0", "result": {"approved": approved}, "id": req_id}
                await self._transport.send(response)

            elif method == NOTIF_GAME_ELICITATION:
                if self.on_elicitation:
                    sid = params.get("sessionId", "")
                    await self.on_elicitation(sid, params)

            elif method == NOTIF_SESSION_ENDED:
                self.state = SessionState.ENDED

    def _finalize(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self.state = SessionState.ENDED

    async def close(self) -> None:
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        self._read_task = None
        self._finalize()
        close = getattr(self._transport, "close", None)
        if close is not None:
            await close()
