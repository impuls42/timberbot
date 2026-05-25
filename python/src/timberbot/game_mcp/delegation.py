"""Delegation MCP tool family: `delegate`, `subagent_reply`, `subagent_status`,
`subagent_wait`, `subagent_cancel`, `subagent_close`, `subagent_list`.

The handlers run inside the FastMCP server's request context. They look up
the calling user via the `X-Timberbot-User-Id` HTTP header on the SSE
connection (threaded through by `_user_message_loop` when it opens the
agent's MCP server config) and route to the right per-user
`AgentConnection` + `SubagentRegistry`.

Phase 1 surface — see `design/subagent-delegation.md` §5 and §9.1 for the
full spec. `subagent_wait_all`, `subagent_transcript`, idle timeout, and
per-call timeout are Phase 2.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import fastmcp

from timberbot.connector.agent_spec import (
    SUBAGENTS,
    AgentSpec,
    render_subagent_bootstrap,
)
from timberbot.connector.session import AgentConnection
from timberbot.connector.subagent import SubagentRegistry, SubagentRun, Turn

log = logging.getLogger("timberbot.game_mcp.delegation")

# Header used to pin a FastMCP request to its originating Timberbot user.
# Set on the SSE MCP server config when the user's ACP session is opened
# (see `user_api/serve.py:_user_message_loop`).
USER_ID_HEADER = "X-Timberbot-User-Id"


@dataclass
class UserState:
    """Per-user serve state the delegation tools need at request time."""
    conn: AgentConnection
    registry: SubagentRegistry
    # The cwd + mcp_servers list to pass when opening a subagent session.
    # Same shape as the main session's so the subagent can call `game.*` tools.
    agent_cwd: str
    mcp_servers: list[dict]


class SubagentBroker:
    """Process-global table of per-user state, looked up by HTTP header.

    Populated by `_user_message_loop` when a user's `AgentConnection` is
    opened, and cleared when that handle is evicted. The MCP tool handlers
    call `lookup_by_request()` to find the calling user — which works
    because `tbot serve` adds `X-Timberbot-User-Id: <user>` to the
    SSE MCP server config it hands to the ACP agent.
    """

    def __init__(self) -> None:
        self._users: dict[str, UserState] = {}

    def register(
        self,
        user_id: str,
        conn: AgentConnection,
        agent_cwd: str,
        mcp_servers: list[dict],
    ) -> SubagentRegistry:
        registry = SubagentRegistry()
        self._users[user_id] = UserState(
            conn=conn,
            registry=registry,
            agent_cwd=agent_cwd,
            mcp_servers=mcp_servers,
        )
        return registry

    async def unregister(self, user_id: str) -> None:
        state = self._users.pop(user_id, None)
        if state is not None:
            await state.registry.close_all()

    def get(self, user_id: str) -> UserState | None:
        return self._users.get(user_id)

    def lookup_by_request(self) -> UserState | None:
        """Read `X-Timberbot-User-Id` from the current HTTP request and
        return that user's state, or None if no header / no match.

        Logs a warning on every miss so a misconfigured caller (no HTTP
        request context, missing header, unknown user) is visible in the
        serve log rather than silently falling through to the tool's
        `"no Timberbot user bound"` error reply.
        """
        from fastmcp.server.dependencies import get_http_request  # noqa: PLC0415
        try:
            req = get_http_request()
        except Exception as exc:  # noqa: BLE001 - no request bound (test stub, etc.)
            log.warning("delegate-family tool called outside HTTP request context: %s", exc)
            return None
        user_id = req.headers.get(USER_ID_HEADER) if req is not None else None
        if not user_id:
            log.warning(
                "delegate-family tool called without %s header — cannot route",
                USER_ID_HEADER,
            )
            return None
        state = self._users.get(user_id)
        if state is None:
            log.warning("delegate-family tool: no registered user %r", user_id)
        return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_for_slug(slug: str) -> AgentSpec | None:
    for s in SUBAGENTS:
        if s.slug == slug:
            return s
    return None


def _summary(run: SubagentRun) -> dict[str, Any]:
    """Shared shape for status/list responses."""
    return {
        "subagent_id": run.subagent_id,
        "agent": run.spec.slug,
        "status": run.status,
        "turns_completed": run.turns_completed,
        "last_active_at": run.last_active_at,
    }


def _last_reply(run: SubagentRun) -> str:
    return run.transcript[-1].agent_reply if run.transcript else ""


def _last_stop_reason(run: SubagentRun) -> str:
    return run.transcript[-1].stop_reason if run.transcript else ""


def _drain_background_turn(task: asyncio.Task[str]) -> None:
    """Done-callback for fire-and-forget `_drive_turn` tasks.

    `delegate(wait=False)` / `subagent_reply(wait=False)` schedule turns
    nobody awaits — without a callback, an unhandled exception (including a
    `CancelledError` from `subagent_cancel`) gets logged by asyncio's
    default handler. `_drive_turn` already records the failure into
    `run.status` + `run.last_error`, so the callback just consumes the
    result. CancelledError is the expected outcome of `subagent_cancel`
    and is silently swallowed; everything else is logged at WARNING.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None or isinstance(exc, asyncio.CancelledError):
        return
    log.warning("background subagent turn failed: %s", exc)


async def _drive_turn(run: SubagentRun, user_message: str, prompt_text: str) -> str:
    """Run one prompt turn on `run.session` and record it in the transcript."""
    run.status = "running"
    started = time.monotonic()
    try:
        reply = await run.session.prompt_awaitable(prompt_text)
        stop_reason = run.session.current_stop_reason or "end_turn"
        run.transcript.append(Turn(
            user_message=user_message,
            agent_reply=reply,
            stop_reason=stop_reason,
            started_at=started,
            ended_at=time.monotonic(),
        ))
        run.status = "completed"
        run.touch()
        return reply
    except asyncio.CancelledError:
        run.status = "cancelled"
        run.touch()
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as run.last_error
        run.status = "errored"
        run.last_error = str(exc)
        run.touch()
        raise


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_delegation_tools(mcp: fastmcp.FastMCP, broker: SubagentBroker) -> None:
    """Register all delegate-family tools on `mcp`.

    Called from `create_mcp_server` once the broker has been constructed.
    Pulled into its own function so tests can register against a throwaway
    FastMCP instance with a stubbed broker.
    """

    @mcp.tool
    async def delegate(
        agent: str,
        task: str,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Open a new subagent run and start the first turn.

        agent: subagent slug — one of 'scout', 'wirer', 'auditor'.
        task: initial instructions; the subagent sees this as the user's first message.
        wait: if True, block until the first turn ends and return the reply.
              if False (default), return immediately with status='running'.
        """
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        spec = _spec_for_slug(agent)
        if spec is None:
            available = [s.slug for s in SUBAGENTS]
            return {
                "error": f"unknown agent: {agent!r}",
                "available": available,
            }

        run = await state.registry.open(
            spec, state.conn,
            cwd=state.agent_cwd, mcp_servers=state.mcp_servers,
        )
        prompt_text = render_subagent_bootstrap(spec) + "\n" + task

        if wait:
            try:
                reply = await _drive_turn(run, task, prompt_text)
            except Exception as exc:  # noqa: BLE001
                return {
                    "subagent_id": run.subagent_id,
                    "status": run.status,
                    "error": str(exc),
                }
            return {
                "subagent_id": run.subagent_id,
                "status": run.status,
                "stop_reason": _last_stop_reason(run),
                "reply": reply,
            }

        # wait=False: kick off the turn as a background task and return.
        # We set `run.status = "running"` here *before* the task starts
        # executing — between `create_task` and this tool's return there's
        # no await, so the wrapped coroutine hasn't run yet and would still
        # show "idle". `_drive_turn` re-asserts "running" on its first line;
        # the duplication is intentional so a `subagent_status` racing this
        # return always sees "running", never an interim "idle".
        run.turn_task = asyncio.create_task(
            _drive_turn(run, task, prompt_text),
            name=f"subagent-turn-{run.subagent_id}",
        )
        run.turn_task.add_done_callback(_drain_background_turn)
        run.status = "running"
        return {"subagent_id": run.subagent_id, "status": "running"}

    @mcp.tool
    async def subagent_reply(
        subagent_id: str,
        message: str,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Send a follow-up turn to an existing subagent. The subagent sees
        `message` as the user's next prompt with full prior context."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        if run.session.is_busy:
            return {
                "subagent_id": subagent_id,
                "error": "busy",
                "status": run.status,
            }

        if wait:
            try:
                reply = await _drive_turn(run, message, message)
            except Exception as exc:  # noqa: BLE001
                return {
                    "subagent_id": subagent_id,
                    "status": run.status,
                    "error": str(exc),
                }
            return {
                "subagent_id": subagent_id,
                "status": run.status,
                "stop_reason": _last_stop_reason(run),
                "reply": reply,
            }

        run.turn_task = asyncio.create_task(
            _drive_turn(run, message, message),
            name=f"subagent-turn-{subagent_id}",
        )
        run.turn_task.add_done_callback(_drain_background_turn)
        run.status = "running"
        return {"subagent_id": subagent_id, "status": "running"}

    @mcp.tool
    async def subagent_status(subagent_id: str) -> dict[str, Any]:
        """Cheap non-blocking peek: returns metadata only — no reply text."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        return _summary(run)

    @mcp.tool
    async def subagent_wait(
        subagent_id: str,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Block until the in-flight turn finishes, or timeout (seconds).

        If the subagent is already idle/completed/errored, returns the last
        reply immediately. `timed_out=True` means the turn is still running.
        """
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}

        if run.turn_task is not None and not run.turn_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(run.turn_task), timeout=timeout)
            except asyncio.TimeoutError:
                return {
                    "subagent_id": subagent_id,
                    "status": run.status,
                    "timed_out": True,
                }
            except asyncio.CancelledError:
                # The turn was cancelled externally (subagent_cancel). The
                # registry already recorded status="cancelled"; fall through
                # to the return below, which surfaces it to the caller.
                pass
            except Exception as exc:  # noqa: BLE001 - `_drive_turn` recorded run.last_error
                log.warning(
                    "subagent_wait saw underlying turn fail for %s: %s",
                    subagent_id, exc,
                )

        return {
            "subagent_id": subagent_id,
            "status": run.status,
            "stop_reason": _last_stop_reason(run),
            "reply": _last_reply(run),
            "last_error": run.last_error,
            "timed_out": False,
        }

    @mcp.tool
    async def subagent_cancel(subagent_id: str) -> dict[str, Any]:
        """Cancel the in-flight turn. Session stays open for follow-ups."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        run = await state.registry.cancel(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        return {"subagent_id": subagent_id, "status": run.status}

    @mcp.tool
    async def subagent_close(subagent_id: str) -> dict[str, Any]:
        """Release the session and drop from the registry. The id is invalid afterwards."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        if state.registry.get(subagent_id) is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        await state.registry.close(subagent_id)
        return {"ok": True, "subagent_id": subagent_id}

    @mcp.tool
    async def subagent_list() -> dict[str, Any]:
        """All subagents currently registered for the calling user."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot user bound to this MCP session"}
        return {"subagents": [_summary(r) for r in state.registry.list()]}
