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


# MCP tool names that mutate game state. Read-only tools (summary, alerts,
# time, weather, prefabs, buildings, etc.) are intentionally excluded so the
# action-feed doesn't drown the chat in routine inspection calls.
_WRITE_TOOL_PREFIXES: tuple[str, ...] = (
    "add_", "clear_", "configure_", "demolish_",
    "mark_", "migrate", "pause_", "unpause_",
    "place_", "plant_", "remove_", "rename_",
    "set_", "unlock_", "update_", "link", "unlink",
)


def _clean_tool_title(title: str) -> str:
    """Strip the Claude SDK's `mcp__<server>__<tool>` framing so the user sees
    just `<tool>`. Pass-through for any title that doesn't match the pattern."""
    if title.startswith("mcp__"):
        _, _, tool = title[len("mcp__"):].partition("__")
        if tool:
            return tool
    return title


def _is_write_tool(name: str) -> bool:
    return any(name.startswith(p) for p in _WRITE_TOOL_PREFIXES)


def _format_tool_input(raw: object) -> str:
    """One-line key=value rendering of a tool's raw_input; capped for readability."""
    if isinstance(raw, dict):
        parts: list[str] = []
        for k, v in list(raw.items())[:6]:
            s = str(v)
            if len(s) > 40:
                s = s[:37] + "…"
            parts.append(f"{k}={s}")
        if len(raw) > 6:
            parts.append("…")
        return ", ".join(parts)
    return str(raw)[:120] if raw is not None else ""


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

    Inbound notifications carry `session_id`; we look up the right `Session`
    on the owning `AgentConnection` and forward to its callbacks. Tool
    permission requests are auto-resolved against the calling session's
    `_allowed_tools`. The deprecated game elicitation extension routes to
    that session's `on_elicitation`.
    """

    def __init__(self, conn: AgentConnection) -> None:
        self._conn = conn

    async def session_update(self, session_id: str, update: object, **_: object) -> None:
        session = self._conn._sessions.get(session_id)
        if session is None:
            log.debug("session/update for unknown session %s; ignoring", session_id)
            return
        # Tool-call lifecycle: emit a one-line "🔧 …" notification once per
        # write-tool call when it reaches a terminal status. Dedup by
        # tool_call_id so we only fire once even though the SDK may send
        # multiple updates (pending → in_progress → completed) per call.
        if isinstance(update, (schema.ToolCallStart, schema.ToolCallProgress, schema.ToolCallUpdate)):
            await self._maybe_emit_tool_action(session, update)
            return
        if not isinstance(update, (schema.AgentMessageChunk, schema.AgentThoughtChunk)):
            return
        content = update.content
        if not isinstance(content, schema.TextContentBlock):
            log.debug("session/update %s: skipping non-text content", type(update).__name__)
            return
        if content.text:
            await session._handle_text_chunk(content.text)

    async def _maybe_emit_tool_action(self, session: Session, update: object) -> None:
        tool_call_id = getattr(update, "tool_call_id", None)
        if not tool_call_id:
            return
        # Remember start payloads so we can resolve the title and input
        # when the terminal `ToolCallProgress` arrives with title=None.
        title = getattr(update, "title", None)
        raw_input = getattr(update, "raw_input", None)
        if title is not None or raw_input is not None:
            prev = session._tool_call_meta.get(tool_call_id, ("", None))
            session._tool_call_meta[tool_call_id] = (
                title or prev[0],
                raw_input if raw_input is not None else prev[1],
            )

        status = getattr(update, "status", None)
        if status not in ("completed", "failed"):
            return
        if tool_call_id in session._emitted_tool_calls:
            return
        resolved_title, resolved_input = session._tool_call_meta.get(tool_call_id, ("", None))
        if not resolved_title:
            return
        clean = _clean_tool_title(resolved_title)
        if not _is_write_tool(clean):
            return  # quiet on read-only inspections
        session._emitted_tool_calls.add(tool_call_id)
        on_tool_action = session.on_tool_action
        if on_tool_action is None:
            return
        args = _format_tool_input(resolved_input)
        emoji = "✅" if status == "completed" else "❌"
        summary = f"{emoji} {clean}({args})" if args else f"{emoji} {clean}"
        with contextlib.suppress(Exception):
            await on_tool_action(session.session_id, summary, status == "completed")

    async def request_permission(
        self, options: list, session_id: str, tool_call: object, **_: object
    ) -> acp.RequestPermissionResponse:
        session = self._conn._sessions.get(session_id)
        approved = bool(session is not None and session._tool_allowed(tool_call))
        option_id = _pick_option(options, approve=approved)
        outcome: schema.AllowedOutcome | schema.DeniedOutcome
        if option_id is not None:
            outcome = schema.AllowedOutcome(outcome="selected", option_id=option_id)
        else:
            outcome = schema.DeniedOutcome(outcome="cancelled")
        return acp.RequestPermissionResponse(outcome=outcome)

    async def ext_notification(self, method: str, params: dict) -> None:
        if method == NOTIF_GAME_ELICITATION:
            sid = str(params.get("sessionId", ""))
            session = self._conn._sessions.get(sid)
            if session is not None and session.on_elicitation is not None:
                await session.on_elicitation(sid, params)

    async def ext_method(self, method: str, params: dict) -> dict:
        raise acp.RequestError.method_not_found(method)

    def on_connect(self, conn: object) -> None:  # noqa: D401 - SDK hook
        pass

    async def read_text_file(self, path: str, session_id: str, **_: object) -> object:
        raise acp.RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, content: str, path: str, session_id: str, **_: object) -> object:
        raise acp.RequestError.method_not_found("fs/write_text_file")


class Session:
    """One conversation thread inside an `AgentConnection`.

    Owns: its `session_id`, allowed-tool scope (per-session, not process-wide),
    streaming/elicitation/tool-action callbacks, per-call dedup state, and the
    in-flight turn future used by `prompt_awaitable`.
    """

    def __init__(
        self,
        conn: AgentConnection,
        session_id: str,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self._conn = conn
        self.session_id = session_id
        self.state: SessionState = SessionState.ACTIVE
        self._allowed_tools: list[str] = list(allowed_tools or [])

        self.on_update: Callable[[str, str], Awaitable[None]] | None = None
        self.on_elicitation: Callable[[str, dict], Awaitable[None]] | None = None
        # (session_id, summary, ok) — fires once per completed/failed write-tool call.
        self.on_tool_action: Callable[[str, str, bool], Awaitable[None]] | None = None

        # Tool-call ids already turned into a chat notification, to dedupe
        # across the (pending → in_progress → completed) update sequence.
        self._emitted_tool_calls: set[str] = set()
        # tool_call_id → (title, raw_input). The Claude SDK only fills these
        # on the initial `ToolCallStart`; the final `ToolCallProgress` that
        # carries `status="completed"` arrives with `title=None`, so we
        # remember the start payload to resolve names on the terminal event.
        self._tool_call_meta: dict[str, tuple[str, object]] = {}

        # prompt_awaitable bookkeeping. _current_turn is the future the caller
        # is awaiting; _reply_buffer collects text chunks for that turn so the
        # caller gets the full reply when the future resolves.
        self._current_turn: asyncio.Future[str] | None = None
        self._reply_buffer: list[str] = []
        self._current_stop_reason: str | None = None

        self._turn_tasks: set[asyncio.Task] = set()
        self._closed = False

    async def _handle_text_chunk(self, text: str) -> None:
        # Accumulate for prompt_awaitable, then forward to user callback.
        if self._current_turn is not None and not self._current_turn.done():
            self._reply_buffer.append(text)
        on_update = self.on_update
        if on_update is not None:
            await on_update(self.session_id, text)

    def _tool_allowed(self, tool_call: object) -> bool:
        return any(
            fnmatch.fnmatch(name, pat)
            for name in _tool_match_names(tool_call)
            for pat in self._allowed_tools
        )

    @property
    def is_busy(self) -> bool:
        """True iff a turn is currently in flight on this session."""
        return self._current_turn is not None and not self._current_turn.done()

    async def prompt(self, text: str) -> None:
        """Fire-and-forget turn: returns immediately, run_turn streams via callbacks."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_turn(text, awaiter=None))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def prompt_awaitable(self, text: str) -> str:
        """Send a turn and wait for the full reply text.

        Raises `RuntimeError("busy")` if a turn is already in flight on this
        session — subagent semantics require explicit `cancel`/`wait` first.
        """
        if self.is_busy:
            raise RuntimeError("busy")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._current_turn = fut
        self._reply_buffer = []
        self._current_stop_reason = None
        task = loop.create_task(self._run_turn(text, awaiter=fut))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        return await fut

    @property
    def current_stop_reason(self) -> str | None:
        """Stop reason of the most recent turn (None until one completes)."""
        return self._current_stop_reason

    async def _run_turn(
        self,
        text: str,
        awaiter: asyncio.Future[str] | None,
    ) -> None:
        assert self._conn._conn is not None
        try:
            result = await self._conn._conn.prompt(
                prompt=[acp.text_block(text)], session_id=self.session_id,
            )
            stop_reason = getattr(result, "stop_reason", None)
            self._current_stop_reason = str(stop_reason) if stop_reason is not None else None
            log.debug("agent prompt turn ended: stop_reason=%s", stop_reason)
            if awaiter is not None and not awaiter.done():
                awaiter.set_result("".join(self._reply_buffer))
        except asyncio.CancelledError:
            if awaiter is not None and not awaiter.done():
                awaiter.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced via logs and the awaiter
            log.warning("agent prompt turn failed: %s", exc)
            # The ACP receive loop dies on framing errors (e.g. asyncio's
            # default 64KB readline limit overrun by a big MCP tool result).
            # When that happens the subprocess often stays alive but the
            # connection is unusable. Demote to ENDED so the message loop
            # evicts this session on the next prompt instead of pretending to
            # be ACTIVE forever, and push a final TextChunk so the user sees
            # why their session went quiet.
            self.state = SessionState.ENDED
            on_update = self.on_update
            if on_update is not None:
                with contextlib.suppress(Exception):
                    await on_update(
                        self.session_id,
                        f"\n\n_agent turn failed: {exc}. Send another message to start a new session._",
                    )
            if awaiter is not None and not awaiter.done():
                awaiter.set_exception(exc)

    async def cancel(self) -> None:
        assert self._conn._conn is not None
        await self._conn._conn.cancel(session_id=self.session_id)
        self.state = SessionState.HALTING

    async def close(self) -> None:
        """Close this session (best-effort ACP `session/close`; not every
        backend implements it). Dropped from the connection's registry; the
        connection itself stays open for other sessions."""
        if self._closed:
            return
        self._closed = True
        for task in list(self._turn_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._turn_tasks.clear()
        conn = self._conn._conn
        if conn is not None:
            close_fn = getattr(conn, "close_session", None)
            if close_fn is not None:
                with contextlib.suppress(Exception):
                    await close_fn(session_id=self.session_id)
        self._conn._sessions.pop(self.session_id, None)
        self.state = SessionState.ENDED


class AgentConnection:
    """One ACP subprocess + stdio connection. Holds many `Session`s.

    Maps onto one `claude-agent-acp` (or `opencode acp`) process. ACP itself
    supports many sessions per connection — every method takes `session_id` —
    so a single connection can host the main chat session and several
    subagent sessions concurrently.
    """

    # asyncio's default StreamReader line limit is 64 KiB. ACP framing is
    # newline-delimited JSON-RPC and a single `session/update` notification
    # can easily exceed that once an MCP tool returns a chunky payload
    # (`/api/prefabs` alone is ~28 KiB, plus the event-envelope wrapper, plus
    # JSON-RPC overhead). When the line overruns, asyncio raises
    # `LimitOverrunError`, the ACP receive loop dies, and the session goes
    # silent without the handle noticing. 16 MiB is comfortable headroom for
    # any single tool response we'd reasonably produce.
    _STDIO_LIMIT = 16 * 1024 * 1024

    def __init__(self, model: str | None = None) -> None:
        self._model = model
        self._client = _ConnectorClient(self)
        self._conn: acp.ClientSideConnection | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._exit_task: asyncio.Task | None = None
        self._sessions: dict[str, Session] = {}
        self.agent_capabilities: object = None
        self._closing = False

    async def start(self, argv: list[str], cwd: str | None = None) -> None:
        """Spawn the agent subprocess and bind an ACP connection to its stdio."""
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=self._STDIO_LIMIT,
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

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[dict],
        allowed_tools: list[str] | None = None,
    ) -> Session:
        """Open a new ACP session on this connection and return it.

        `allowed_tools` is per-session — different sessions on the same
        connection can have different scopes (the main chat gets `game.*`,
        a subagent may get only `game.find_placement`).
        """
        assert self._conn is not None
        abs_cwd = str(Path(cwd).resolve())
        servers = [_to_mcp_server(s) for s in mcp_servers]
        result = await self._conn.new_session(cwd=abs_cwd, mcp_servers=servers)
        sid = result.session_id
        if self._model:
            await self._set_model(sid, self._model)
        session = Session(self, sid, allowed_tools=allowed_tools)
        self._sessions[sid] = session
        return session

    async def _set_model(self, session_id: str, model: str) -> None:
        """Pin a session's model post-handshake.

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
        # The agent died on its own; reflect that on every live session and
        # free the connection's tasks.
        for session in self._sessions.values():
            session.state = SessionState.ENDED
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
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                await session.close()
        self._sessions.clear()
        self._exit_task = None
        self._stderr_task = None
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
