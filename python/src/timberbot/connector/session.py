from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path

from timberbot.connector.protocol import (
    ACP_VERSION,
    METHOD_INITIALIZE,
    METHOD_SESSION_CANCEL,
    METHOD_SESSION_NEW,
    METHOD_SESSION_PROMPT,
    METHOD_SESSION_REQUEST_PERMISSION,
    METHOD_SESSION_SET_MODEL,
    NOTIF_GAME_ELICITATION,
    NOTIF_SESSION_UPDATE,
    build_notification,
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
        model: str | None = None,
    ) -> None:
        self._transport = transport
        self._allowed_tools: list[str] = allowed_tools or []
        self._model = model
        self._next_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}

        self.state: SessionState = SessionState.PENDING
        self.session_id: str | None = None
        self.agent_capabilities: dict = {}
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
        result = await self._request(
            METHOD_INITIALIZE,
            {
                "protocolVersion": ACP_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
            timeout=30.0,
        )
        self.agent_capabilities = result.get("agentCapabilities") or {}
        self.state = SessionState.ACTIVE

    async def new_session(self, cwd: str, mcp_servers: list[dict]) -> str:
        # The spec requires an absolute cwd; resolve relative paths so agents
        # that validate this (the bridge included) accept the request.
        abs_cwd = str(Path(cwd).resolve())
        result = await self._request(
            METHOD_SESSION_NEW,
            {"cwd": abs_cwd, "mcpServers": mcp_servers},
        )
        self.session_id = result["sessionId"]
        if self._model:
            await self._set_model(self.session_id, self._model)
        return self.session_id

    async def _set_model(self, session_id: str, model: str) -> None:
        """Pin the session model post-handshake.

        `session/set_model` is unstable and not implemented by every backend
        (opencode sets its model via argv instead). A JSON-RPC error is a soft
        failure: the agent keeps its default model and the session still works.
        """
        try:
            await self._request(
                METHOD_SESSION_SET_MODEL,
                {"sessionId": session_id, "modelId": model},
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001 - any failure here is non-fatal
            log.warning(
                "agent did not accept session/set_model (model=%s): %s; "
                "continuing with the agent's default model",
                model,
                exc,
            )

    async def prompt(self, session_id: str, text: str) -> None:
        """Dispatch a prompt turn without blocking on its completion.

        Under standard ACP `session/prompt` is a request that only resolves when
        the whole turn ends (its `stopReason` is the end-of-turn signal), while
        `session/update` notifications stream in meanwhile. Awaiting it inline
        would freeze the caller's message loop for the entire turn and make
        `/cancel` unresponsive, so we send the request, capture the turn outcome
        via a callback, and return immediately. The read loop resolves the
        pending future when the turn finishes.
        """
        req_id = self._alloc_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        fut.add_done_callback(self._log_turn_end)
        await self._transport.send(
            build_request(
                req_id,
                METHOD_SESSION_PROMPT,
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            )
        )

    def _log_turn_end(self, fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            log.warning("agent prompt turn failed: %s", exc)
            return
        result = fut.result()
        if isinstance(result, dict):
            log.debug("agent prompt turn ended: stopReason=%s", result.get("stopReason"))

    async def cancel(self, session_id: str) -> None:
        # `session/cancel` is a notification (no response). The in-flight
        # `session/prompt` resolves on its own with stopReason "cancelled".
        await self._transport.send(
            build_notification(METHOD_SESSION_CANCEL, {"sessionId": session_id})
        )
        self.state = SessionState.HALTING

    async def read_loop(self) -> None:
        self._read_task = asyncio.current_task()
        while True:
            msg = await self._transport.recv_line()
            if msg is None:
                self._finalize()
                break

            # A response to one of our requests (has an id, no method).
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
                await self._handle_session_update(params)

            elif method == METHOD_SESSION_REQUEST_PERMISSION:
                await self._handle_permission(msg.get("id"), params)

            elif method == NOTIF_GAME_ELICITATION and self.on_elicitation:
                await self.on_elicitation(params.get("sessionId", ""), params)

    async def _handle_session_update(self, params: dict) -> None:
        if not self.on_update:
            return
        sid = params.get("sessionId", "")
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind not in ("agent_message_chunk", "agent_thought_chunk"):
            return
        content = update.get("content") or {}
        if content.get("type") != "text":
            # Image / resource blocks aren't rendered by Telegram today.
            log.debug("session/update %s: skipping %r content", kind, content.get("type"))
            return
        text = content.get("text", "")
        if text:
            await self.on_update(sid, text)

    async def _handle_permission(self, req_id: object, params: dict) -> None:
        tool_call = params.get("toolCall") or {}
        options = params.get("options") or []
        approved = self._tool_allowed(tool_call)
        option_id = self._pick_option(options, approve=approved)
        if option_id is not None:
            outcome: dict = {"outcome": "selected", "optionId": option_id}
        else:
            # No usable option offered — decline by reporting cancellation.
            outcome = {"outcome": "cancelled"}
        await self._transport.send(
            {"jsonrpc": "2.0", "id": req_id, "result": {"outcome": outcome}}
        )

    def _tool_allowed(self, tool_call: dict) -> bool:
        return any(
            fnmatch.fnmatch(name, pat)
            for name in self._tool_match_names(tool_call)
            for pat in self._allowed_tools
        )

    @staticmethod
    def _tool_match_names(tool_call: dict) -> set[str]:
        """Candidate names for a tool call to match against `allowed_tools`.

        Standard ACP tool calls carry a human-readable `title`, not the raw MCP
        tool name. The Claude Agent SDK titles MCP tools `mcp__<server>__<tool>`,
        so we also expose the normalized `<server>.<tool>` form. That keeps
        globs like `game.*` matching the game MCP server's tools.
        """
        names: set[str] = set()
        title = tool_call.get("title")
        if isinstance(title, str) and title:
            names.add(title)
            if title.startswith("mcp__"):
                server, sep, tool = title[len("mcp__"):].partition("__")
                if sep:
                    names.add(f"{server}.{tool}")
        return names

    @staticmethod
    def _pick_option(options: list[dict], approve: bool) -> str | None:
        """Choose a permission option's id by its spec `kind`.

        Matching on `kind` (an ACP enum) rather than the backend-specific
        `optionId` string keeps this robust across agents. Prefer the
        single-shot variant so every call is re-evaluated.
        """
        wanted = ("allow_once", "allow_always") if approve else ("reject_once", "reject_always")
        for kind in wanted:
            for opt in options:
                if opt.get("kind") == kind:
                    return opt.get("optionId")
        prefix = "allow" if approve else "reject"
        for opt in options:
            if str(opt.get("kind", "")).startswith(prefix):
                return opt.get("optionId")
        return None

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
