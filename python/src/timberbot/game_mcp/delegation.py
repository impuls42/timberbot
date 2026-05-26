"""Delegation MCP tool family.

Surface:

- `delegate`, `subagent_reply` — open / continue a subagent run.
- `subagent_status`, `subagent_list` — non-blocking introspection.
- `subagent_wait`, `subagent_wait_all` — block on one / every in-flight turn.
- `subagent_transcript` — full conversation history for one run.
- `subagent_cancel`, `subagent_close` — interrupt / release.

`tbot serve` binds the broker to a single dialog at startup, so the
broker holds exactly one `UserState` and `lookup_by_request()` is a
plain getter — no per-request HTTP routing, no dialog→state lookup
table. The tool handlers retrieve that state and operate on the
dialog's `AgentConnection` + `SubagentRegistry`.

See `design/subagent-delegation.md` §5 for the tool surface and §9 for the
phase split.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import fastmcp

from timberbot.connector.agent_spec import (
    SUBAGENTS,
    AgentSpec,
    render_subagent_bootstrap,
)
from timberbot.connector.session import AgentConnection, Session
from timberbot.connector.subagent import SubagentRegistry, SubagentRun, Turn

# Default per-call timeout for `delegate` and `subagent_reply`. Surfaces as
# `status="errored", last_error="timeout after Ns"`. Configurable per
# UserState — `_user_message_loop` plumbs `ServeConfig.subagent_call_timeout_s`
# in when it registers each user.
DEFAULT_CALL_TIMEOUT_S = 60.0

# Default idle-timeout for the sweeper task — see §6.1 of the design doc.
# Configurable per UserState; the sweeper itself lives on `SubagentRegistry`.
DEFAULT_IDLE_TIMEOUT_S = 600.0

log = logging.getLogger("timberbot.game_mcp.delegation")


@dataclass
class UserState:
    """The single-dialog serve state the delegation tools need at request time."""
    conn: AgentConnection
    registry: SubagentRegistry
    # The cwd + mcp_servers list to pass when opening a subagent session.
    # Same shape as the main session's so the subagent can call `game.*` tools.
    agent_cwd: str
    mcp_servers: list[dict]
    # Per-call timeout (seconds) for one prompt turn in `delegate(wait=True)`
    # / `subagent_reply(wait=True)` and the background turns from `wait=False`.
    # On timeout the run is marked `errored` with `last_error="timeout after Ns"`.
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    # Factory invoked by the registry when a new subagent session is opened.
    # Returns the (on_tool_action, on_status_change) callbacks the session
    # should fire. Lets `_user_message_loop` wire subagent events into
    # Telegram with a `[<subagent_id>]` prefix without the registry having
    # to know about the user-API layer.
    bind_subagent_callbacks: Callable[
        [SubagentRun, Session], Awaitable[None] | None,
    ] | None = None
    # Status-change observer fired on every `SubagentRun.status` transition.
    # `(run, prev_status, new_status, detail)`. Lets the serve loop emit a
    # `SubagentStatusChange` protocol message into Telegram.
    on_status_change: Callable[
        [SubagentRun, str, str, str | None], Awaitable[None] | None,
    ] | None = None


class SubagentBroker:
    """Single-dialog state holder for the delegate-family MCP tools.

    `tbot serve` calls `bind(...)` once at startup, after which
    `lookup_by_request()` (called by every tool handler) returns the
    bound `UserState`. `unbind()` runs on teardown to close any live
    subagent sessions. No per-request HTTP routing — there's only one
    dialog the bot is bound to, so the lookup is a plain getter.
    """

    def __init__(self) -> None:
        self._state: UserState | None = None

    def bind(
        self,
        conn: AgentConnection,
        agent_cwd: str,
        mcp_servers: list[dict],
        *,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        bind_subagent_callbacks: Callable[
            [SubagentRun, Session], Awaitable[None] | None,
        ] | None = None,
        on_status_change: Callable[
            [SubagentRun, str, str, str | None], Awaitable[None] | None,
        ] | None = None,
    ) -> SubagentRegistry:
        registry = SubagentRegistry(idle_timeout_s=idle_timeout_s)
        # Hook the registry's status emitter to the user-supplied observer so
        # every `run.status` transition surfaces via `SubagentStatusChange`.
        registry.on_status_change = on_status_change  # type: ignore[assignment]
        self._state = UserState(
            conn=conn,
            registry=registry,
            agent_cwd=agent_cwd,
            mcp_servers=mcp_servers,
            call_timeout_s=call_timeout_s,
            bind_subagent_callbacks=bind_subagent_callbacks,
            on_status_change=on_status_change,
        )
        registry.start_idle_sweeper()
        return registry

    async def unbind(self) -> None:
        state = self._state
        self._state = None
        if state is not None:
            await state.registry.close_all()
            await state.registry.stop_idle_sweeper()

    def state(self) -> UserState | None:
        return self._state

    def lookup_by_request(self) -> UserState | None:
        """Return the bound UserState, or None if `bind()` hasn't been
        called yet (only happens in tests or before `tbot serve` finishes
        startup wiring).

        Single-dialog mode collapses what used to be a header-based
        lookup table into a plain getter. The MCP tool handlers' "no
        Timberbot dialog bound" error path stays for the corner case
        where a tool is called before `bind()` completes.
        """
        return self._state


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


async def _drive_turn(
    run: SubagentRun,
    user_message: str,
    prompt_text: str,
    *,
    timeout_s: float,
    registry: SubagentRegistry | None = None,
) -> str:
    """Run one prompt turn on `run.session` and record it in the transcript.

    `timeout_s` bounds the prompt — if the model takes longer the turn is
    cancelled, the run transitions to `errored` with `last_error="timeout"`,
    and the underlying `session.prompt_awaitable` task is cancelled so the
    session goes back to idle.

    `registry` is the run's owning registry, used to push a subagent event
    onto its dialog queue when the turn ends. Optional for tests that
    drive `_drive_turn` directly with a fake.
    """
    run.set_status("running")
    started = time.monotonic()
    try:
        reply = await asyncio.wait_for(
            run.session.prompt_awaitable(prompt_text),
            timeout=timeout_s,
        )
        stop_reason = run.session.current_stop_reason or "end_turn"
        run.transcript.append(Turn(
            user_message=user_message,
            agent_reply=reply,
            stop_reason=stop_reason,
            started_at=started,
            ended_at=time.monotonic(),
        ))
        run.set_status("completed")
        run.touch()
        if registry is not None:
            registry.push_event(run, kind="turn_completed", stop_reason=stop_reason)
        return reply
    except asyncio.TimeoutError:
        run.last_error = f"timeout after {timeout_s:g}s"
        run.set_status("errored", detail=run.last_error)
        run.touch()
        if registry is not None:
            registry.push_event(run, kind="turn_errored")
        # The wait_for cancellation already propagated to prompt_awaitable.
        # Tell the agent runtime so it stops the underlying ACP turn too —
        # otherwise the model would keep generating into a session nobody
        # is reading from.
        with contextlib.suppress(Exception):
            await run.session.cancel()
        raise
    except asyncio.CancelledError:
        run.set_status("cancelled")
        run.touch()
        if registry is not None:
            registry.push_event(run, kind="turn_cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as run.last_error
        run.last_error = str(exc)
        run.set_status("errored", detail=run.last_error)
        run.touch()
        if registry is not None:
            registry.push_event(run, kind="turn_errored")
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
            return {"error": "no Timberbot dialog bound to this MCP session"}
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
        if state.bind_subagent_callbacks is not None:
            result = state.bind_subagent_callbacks(run, run.session)
            if asyncio.iscoroutine(result):
                await result
        prompt_text = render_subagent_bootstrap(spec) + "\n" + task

        if wait:
            try:
                reply = await _drive_turn(
                    run, task, prompt_text,
                    timeout_s=state.call_timeout_s, registry=state.registry,
                )
            except asyncio.CancelledError:
                # A concurrent `subagent_cancel` aborted this turn. `_drive_turn`
                # already set status="cancelled" before re-raising. Surface that
                # cleanly to the caller instead of letting CancelledError escape
                # to the MCP framework (Py 3.11+ raises that as BaseException,
                # which our generic `except Exception` below would miss).
                return {
                    "subagent_id": run.subagent_id,
                    "status": run.status,
                    "cancelled": True,
                }
            except Exception as exc:  # noqa: BLE001
                # Prefer `run.last_error` — `_drive_turn` writes a concrete
                # message there (e.g. "timeout after 60s"), whereas some
                # exceptions like `asyncio.TimeoutError` stringify as empty.
                return {
                    "subagent_id": run.subagent_id,
                    "status": run.status,
                    "error": run.last_error or str(exc) or type(exc).__name__,
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
            _drive_turn(
                run, task, prompt_text,
                timeout_s=state.call_timeout_s, registry=state.registry,
            ),
            name=f"subagent-turn-{run.subagent_id}",
        )
        run.turn_task.add_done_callback(_drain_background_turn)
        run.set_status("running")
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
            return {"error": "no Timberbot dialog bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        # Busy check has to cover *both* the case where the wrapped
        # coroutine is currently mid-prompt (`session.is_busy`) AND the case
        # where `_drive_turn` was just scheduled by `delegate(wait=False)` /
        # `subagent_reply(wait=False)` and hasn't started executing yet.
        # In that second window `session._current_turn` is still None — only
        # `run.turn_task` is set synchronously by `create_task`. Without the
        # turn_task check, a rapid follow-up `subagent_reply` would slip past
        # the guard, hit `prompt_awaitable`'s own busy assertion inside the
        # second `_drive_turn`, and pollute `run.status` with "errored"
        # while the first turn is still alive.
        if (
            (run.turn_task is not None and not run.turn_task.done())
            or run.session.is_busy
        ):
            return {
                "subagent_id": subagent_id,
                "error": "busy",
                "status": run.status,
            }

        if wait:
            try:
                reply = await _drive_turn(
                    run, message, message,
                    timeout_s=state.call_timeout_s, registry=state.registry,
                )
            except asyncio.CancelledError:
                # Concurrent `subagent_cancel` — see the matching branch in
                # `delegate(wait=True)` for the full rationale.
                return {
                    "subagent_id": subagent_id,
                    "status": run.status,
                    "cancelled": True,
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "subagent_id": subagent_id,
                    "status": run.status,
                    "error": run.last_error or str(exc) or type(exc).__name__,
                }
            return {
                "subagent_id": subagent_id,
                "status": run.status,
                "stop_reason": _last_stop_reason(run),
                "reply": reply,
            }

        run.turn_task = asyncio.create_task(
            _drive_turn(
                run, message, message,
                timeout_s=state.call_timeout_s, registry=state.registry,
            ),
            name=f"subagent-turn-{subagent_id}",
        )
        run.turn_task.add_done_callback(_drain_background_turn)
        run.set_status("running")
        return {"subagent_id": subagent_id, "status": "running"}

    @mcp.tool
    async def subagent_status(subagent_id: str) -> dict[str, Any]:
        """Cheap non-blocking peek: returns metadata only — no reply text."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot dialog bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        # An actively-polled run is by definition "in use" — touch
        # last_active_at so the idle sweeper doesn't reclaim a subagent the
        # main agent is still consulting. Matches design doc §6.1.
        run.touch()
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
            return {"error": "no Timberbot dialog bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        # The caller is actively waiting on this run — keep it alive against
        # the idle sweeper. Same rationale as `subagent_status` above.
        run.touch()

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
            return {"error": "no Timberbot dialog bound to this MCP session"}
        run = await state.registry.cancel(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        return {"subagent_id": subagent_id, "status": run.status}

    @mcp.tool
    async def subagent_close(subagent_id: str) -> dict[str, Any]:
        """Release the session and drop from the registry. The id is invalid afterwards."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot dialog bound to this MCP session"}
        if state.registry.get(subagent_id) is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        await state.registry.close(subagent_id)
        return {"ok": True, "subagent_id": subagent_id}

    @mcp.tool
    async def subagent_list() -> dict[str, Any]:
        """All subagents currently registered for the calling user."""
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot dialog bound to this MCP session"}
        return {"subagents": [_summary(r) for r in state.registry.list()]}

    @mcp.tool
    async def subagent_wait_all(timeout: float = 60.0) -> dict[str, Any]:
        """Block until every in-flight subagent reaches a non-running state.

        Returns the result of every run currently in the registry — including
        idle / completed / errored ones — in the order they finish (or the
        order they were already done at call time). `timed_out=True` means
        at least one turn was still running when the overall timeout fired.
        Pairs with `delegate(..., wait=False)` for fan-out workflows: fire
        several delegations early in your turn, then collect with one
        `subagent_wait_all` instead of polling each via `subagent_wait`.
        """
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot dialog bound to this MCP session"}

        runs = state.registry.list()
        if not runs:
            return {"results": [], "timed_out": False}

        in_flight = [r for r in runs if r.turn_task is not None and not r.turn_task.done()]
        timed_out = False
        if in_flight:
            tasks = [asyncio.shield(r.turn_task) for r in in_flight]  # type: ignore[arg-type]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
            except asyncio.CancelledError:
                # Cancellation here means the wait_all caller's MCP request
                # was aborted, not the runs themselves. The underlying runs
                # may still be in flight, so report timed_out=True — same
                # signal as the explicit `wait_for` timeout — rather than
                # implying everything is settled.
                timed_out = True

        results = []
        for r in state.registry.list():
            results.append({
                "subagent_id": r.subagent_id,
                "agent": r.spec.slug,
                "status": r.status,
                "stop_reason": _last_stop_reason(r),
                "reply": _last_reply(r),
                "last_error": r.last_error,
            })
        return {"results": results, "timed_out": timed_out}

    @mcp.tool
    async def subagent_transcript(subagent_id: str) -> dict[str, Any]:
        """Return the full conversation history of one subagent.

        Heavier than the other introspection tools — every `(user_message,
        agent_reply)` pair plus its `stop_reason`. Intended for edge cases
        where the main agent needs to re-read what was discussed (e.g. after
        a context reset, or to extract structured data from an earlier turn).
        Prefer `subagent_status` / `subagent_list` for quick checks.
        """
        state = broker.lookup_by_request()
        if state is None:
            return {"error": "no Timberbot dialog bound to this MCP session"}
        run = state.registry.get(subagent_id)
        if run is None:
            return {"error": f"unknown subagent_id: {subagent_id!r}"}
        return {
            "subagent_id": subagent_id,
            "agent": run.spec.slug,
            "status": run.status,
            "turns": [
                {
                    "user_message": t.user_message,
                    "agent_reply": t.agent_reply,
                    "stop_reason": t.stop_reason,
                    "started_at": t.started_at,
                    "ended_at": t.ended_at,
                }
                for t in run.transcript
            ],
        }
