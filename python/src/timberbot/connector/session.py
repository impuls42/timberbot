from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import cast

import acp
from acp import schema

from timberbot.connector.protocol import NOTIF_GAME_ELICITATION

log = logging.getLogger("timberbot.connector")


class SessionState(Enum):
    PENDING = "pending"
    ACTIVE = "active"
    HALTING = "halting"
    ENDED = "ended"


def _client_capabilities() -> schema.ClientCapabilities:
    # We don't service the agent's fs/terminal requests, so advertise none.
    # `auth=None` sidesteps a noisy serializer warning from the SDK's default.
    return schema.ClientCapabilities(
        fs=schema.FileSystemCapabilities(read_text_file=False, write_text_file=False),
        terminal=False,
        auth=None,
    )


def _to_mcp_server(spec: dict) -> schema.HttpMcpServer | schema.SseMcpServer | schema.McpServerStdio:
    """Convert a plain mcpServer dict (as `serve.py` builds) to an SDK model."""
    kind = spec.get("type")
    headers = [schema.HttpHeader(name=h["name"], value=h["value"]) for h in spec.get("headers", [])]
    if kind == "http":
        return schema.HttpMcpServer(type="http", name=spec["name"], url=spec["url"], headers=headers)
    if kind == "sse":
        return schema.SseMcpServer(type="sse", name=spec["name"], url=spec["url"], headers=headers)
    return schema.McpServerStdio(
        name=spec["name"],
        command=spec["command"],
        args=spec.get("args", []),
        env=[schema.EnvVariable(name=e["name"], value=e["value"]) for e in spec.get("env", [])],
    )


def _tool_match_names(tool_call: object) -> set[str]:
    """Candidate names for a tool call to match against `allowed_tools`.

    ACP tool calls carry a human-readable `title`, not the raw MCP tool name.
    The Claude Agent SDK titles MCP tools `mcp__<server>__<tool>`, so we also
    expose the normalized `<server>.<tool>` form to keep globs like `game.*`
    matching the game MCP server's tools.
    """
    names: set[str] = set()
    title = getattr(tool_call, "title", None)
    if isinstance(title, str) and title:
        names.add(title)
        if title.startswith("mcp__"):
            server, sep, tool = title[len("mcp__"):].partition("__")
            if sep:
                names.add(f"{server}.{tool}")
    return names


def _pick_option(options: list, approve: bool) -> str | None:
    """Choose a permission option's id by its spec `kind`.

    Matching on `kind` (an ACP enum) rather than the backend-specific
    `optionId` string keeps this robust across agents; prefer the single-shot
    variant so every call is re-evaluated.
    """
    wanted = ("allow_once", "allow_always") if approve else ("reject_once", "reject_always")
    prefix = "allow" if approve else "reject"

    def kind_of(opt: object) -> str:
        k = getattr(opt, "kind", "")
        return str(getattr(k, "value", k))

    for want in wanted:
        for opt in options:
            if kind_of(opt) == want:
                return getattr(opt, "option_id", None)
    for opt in options:
        if kind_of(opt).startswith(prefix):
            return getattr(opt, "option_id", None)
    return None


class _ConnectorClient:
    """ACP `Client` (editor side): the agent calls back into this.

    Streaming updates are forwarded to `handle.on_update`; tool permission
    requests are auto-resolved against `handle._allowed_tools`; the deprecated
    game elicitation extension is routed to `handle.on_elicitation`. We advertise
    no fs/terminal support, so those requests never arrive — the fs methods below
    exist only to satisfy the SDK router and refuse defensively if ever called.
    """

    def __init__(self, handle: SessionHandle) -> None:
        self._handle = handle

    async def session_update(self, session_id: str, update: object, **_: object) -> None:
        on_update = self._handle.on_update
        if on_update is None:
            return
        if not isinstance(update, (schema.AgentMessageChunk, schema.AgentThoughtChunk)):
            return
        content = update.content
        if not isinstance(content, schema.TextContentBlock):
            log.debug("session/update %s: skipping non-text content", type(update).__name__)
            return
        if content.text:
            await on_update(session_id, content.text)

    async def request_permission(
        self, options: list, session_id: str, tool_call: object, **_: object
    ) -> acp.RequestPermissionResponse:
        approved = self._handle._tool_allowed(tool_call)
        option_id = _pick_option(options, approve=approved)
        outcome: schema.AllowedOutcome | schema.DeniedOutcome
        if option_id is not None:
            outcome = schema.AllowedOutcome(outcome="selected", option_id=option_id)
        else:
            # No usable option offered — decline by reporting cancellation.
            outcome = schema.DeniedOutcome(outcome="cancelled")
        return acp.RequestPermissionResponse(outcome=outcome)

    async def ext_notification(self, method: str, params: dict) -> None:
        if method == NOTIF_GAME_ELICITATION and self._handle.on_elicitation:
            await self._handle.on_elicitation(params.get("sessionId", ""), params)

    async def ext_method(self, method: str, params: dict) -> dict:
        raise acp.RequestError.method_not_found(method)

    def on_connect(self, conn: object) -> None:  # noqa: D401 - SDK hook
        pass

    async def read_text_file(self, path: str, session_id: str, **_: object) -> object:
        raise acp.RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, content: str, path: str, session_id: str, **_: object) -> object:
        raise acp.RequestError.method_not_found("fs/write_text_file")


class SessionHandle:
    def __init__(
        self,
        allowed_tools: list[str] | None = None,
        model: str | None = None,
    ) -> None:
        self._allowed_tools: list[str] = allowed_tools or []
        self._model = model

        self.state: SessionState = SessionState.PENDING
        self.session_id: str | None = None
        self.agent_capabilities: object = None
        self.on_update: Callable[[str, str], Awaitable[None]] | None = None
        self.on_elicitation: Callable[[str, dict], Awaitable[None]] | None = None

        self._client = _ConnectorClient(self)
        self._conn: acp.ClientSideConnection | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._exit_task: asyncio.Task | None = None
        self._turn_tasks: set[asyncio.Task] = set()
        self._closing = False

    async def start(self, argv: list[str], cwd: str | None = None) -> None:
        """Spawn the agent subprocess and bind an ACP connection to its stdio."""
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        # connect_to_agent wants the agent's stdin (to write to) and stdout
        # (to read from); it runs its own receive loop on construction.
        self._conn = acp.connect_to_agent(
            cast(acp.Client, self._client), self._proc.stdin, self._proc.stdout
        )
        loop = asyncio.get_running_loop()
        self._stderr_task = loop.create_task(self._drain_stderr())
        self._exit_task = loop.create_task(self._watch_exit())

    async def initialize(self) -> None:
        assert self._conn is not None
        result = await self._conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=_client_capabilities(),
        )
        self.agent_capabilities = getattr(result, "agent_capabilities", None)
        self.state = SessionState.ACTIVE

    async def new_session(self, cwd: str, mcp_servers: list[dict]) -> str:
        assert self._conn is not None
        # The spec requires an absolute cwd.
        abs_cwd = str(Path(cwd).resolve())
        servers = [_to_mcp_server(s) for s in mcp_servers]
        result = await self._conn.new_session(cwd=abs_cwd, mcp_servers=servers)
        self.session_id = result.session_id
        if self._model:
            await self._set_model(self.session_id, self._model)
        return self.session_id

    async def _set_model(self, session_id: str, model: str) -> None:
        """Pin the session model post-handshake.

        `session/set_model` is unstable and not implemented by every backend
        (opencode sets its model via argv instead), so any error here is a soft
        failure: the agent keeps its default model and the session still works.
        """
        assert self._conn is not None
        try:
            await self._conn.set_session_model(model_id=model, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - any failure here is non-fatal
            log.warning(
                "agent did not accept session/set_model (model=%s): %s; "
                "continuing with the agent's configured model",
                model,
                exc,
            )

    async def prompt(self, session_id: str, text: str) -> None:
        """Dispatch a prompt turn without blocking on its completion.

        `ClientSideConnection.prompt` only resolves when the whole turn ends
        (its `stop_reason` is the end-of-turn signal) while `session/update`
        notifications stream in meanwhile. Awaiting it inline would freeze the
        caller's message loop for the entire turn and make `/cancel`
        unresponsive, so we run it as a background task.
        """
        task = asyncio.get_running_loop().create_task(self._run_turn(session_id, text))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_turn(self, session_id: str, text: str) -> None:
        assert self._conn is not None
        try:
            result = await self._conn.prompt(prompt=[acp.text_block(text)], session_id=session_id)
            log.debug("agent prompt turn ended: stop_reason=%s", getattr(result, "stop_reason", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced via logs, not fatal to the loop
            log.warning("agent prompt turn failed: %s", exc)

    async def cancel(self, session_id: str) -> None:
        assert self._conn is not None
        await self._conn.cancel(session_id=session_id)
        self.state = SessionState.HALTING

    def _tool_allowed(self, tool_call: object) -> bool:
        return any(
            fnmatch.fnmatch(name, pat)
            for name in _tool_match_names(tool_call)
            for pat in self._allowed_tools
        )

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            log.warning("agent stderr: %s", line.decode(errors="replace").rstrip())

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        await self._proc.wait()
        if self._closing:
            return
        # The agent died on its own; reflect that and free the connection's tasks.
        self.state = SessionState.ENDED
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()

    async def close(self) -> None:
        self._closing = True
        for task in (self._exit_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for task in list(self._turn_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._turn_tasks.clear()
        self._exit_task = None
        self._stderr_task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
        self.state = SessionState.ENDED
