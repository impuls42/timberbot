"""Subagent registry and lifecycle for `tbot serve`.

A `SubagentRun` is one delegation: a code-defined `AgentSpec`, the ACP
`Session` running it, the transcript of `(user_message, agent_reply)` turns,
and a status state machine (idle → running → completed | errored | cancelled
→ closed).

`SubagentRegistry` is single-dialog — `tbot serve` binds to one Telegram
chat at startup and the broker holds exactly one registry. The
`delegate(...)` MCP tool retrieves it via
`SubagentBroker.lookup_by_request()` (a plain getter in single-dialog
mode; see `game_mcp/delegation.py`) and operates on that registry.

Phase 1: open, get, close, list, status state machine, ID collision retry.
Phase 2 (this file): idle-timeout sweeper task and a status-change observer
the serve loop hooks for Telegram surfacing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from timberbot.connector.agent_spec import AgentSpec
from timberbot.connector.session import AgentConnection, Session

log = logging.getLogger("timberbot.connector.subagent")

# Status the model and the MCP tool surface speak in. See
# `design/subagent-delegation.md` §4.3 for the transitions.
SubagentStatus = Literal[
    "idle", "running", "completed", "errored", "cancelled", "closed",
]


@dataclass
class Turn:
    """One (prompt → reply) exchange inside a subagent run."""
    user_message: str
    agent_reply: str
    stop_reason: str
    started_at: float
    ended_at: float


@dataclass
class SubagentRun:
    subagent_id: str
    spec: AgentSpec
    session: Session
    status: SubagentStatus = "idle"
    transcript: list[Turn] = field(default_factory=list)
    last_error: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    # Task wrapping the in-flight prompt turn. None when the run is idle.
    # Lives on the run (not the Session) because the delegation layer needs
    # to await it from outside the session-owning coroutine — `subagent_wait`
    # blocks on it, `subagent_cancel` cancels it, and the eviction path
    # cancels every run's task on main-session teardown.
    turn_task: asyncio.Task[str] | None = None
    # Optional observer invoked on every status transition (prev, new, detail).
    # The registry binds this to its own `on_status_change` so the serve loop
    # can surface status flips via `SubagentStatusChange` protocol messages.
    _on_status_change: Callable[
        [SubagentRun, str, str, str | None], Awaitable[None] | None,
    ] | None = field(default=None, repr=False, compare=False)

    @property
    def turns_completed(self) -> int:
        return len(self.transcript)

    def touch(self) -> None:
        self.last_active_at = time.monotonic()

    def set_status(self, new_status: SubagentStatus, detail: str | None = None) -> None:
        """Transition `status` and fire `_on_status_change` if it differs.

        Synchronous setter that schedules the observer (which may be a
        coroutine) on the running loop. Idempotent — assigning the same
        status doesn't refire.
        """
        prev = self.status
        if prev == new_status:
            return
        self.status = new_status
        observer = self._on_status_change
        if observer is None:
            return
        try:
            result = observer(self, prev, new_status, detail)
        except Exception:  # noqa: BLE001 - observer errors must not break the turn
            log.exception("subagent status observer raised for %s", self.subagent_id)
            return
        if asyncio.iscoroutine(result):
            # Fire-and-forget — we don't await observers from inside
            # set_status (which is called from sync paths like cancel()
            # too).
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No loop running (test / shutdown). Drop the awaitable.
                result.close()
                return
            task = loop.create_task(result, name=f"subagent-status-{self.subagent_id}")
            task.add_done_callback(_swallow_observer_exc)


def _swallow_observer_exc(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("subagent status observer task failed: %s", exc)


def _make_subagent_id(slug: str, existing: set[str], retries: int = 3) -> str:
    """`<slug>-<4 hex>` with collision retry. Raises after `retries` attempts."""
    for _ in range(retries):
        nonce = secrets.token_hex(2)  # 4 hex chars = 16 bits = 65 536 IDs/slug
        candidate = f"{slug}-{nonce}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(
        f"subagent_id collision: {retries} retries exhausted for slug {slug!r}"
    )


# Statuses eligible for the idle sweeper. Running turns are never reclaimed.
_SWEEPABLE: frozenset[str] = frozenset(
    ("idle", "completed", "errored", "cancelled"),
)


class SubagentRegistry:
    """Per-user dict of live `SubagentRun`s.

    Idle sweeper (`start_idle_sweeper`) periodically closes runs whose
    `last_active_at` is older than `idle_timeout_s` AND whose status is in
    `_SWEEPABLE`. The registry doesn't own the `AgentConnection` — the
    serve loop does — so opening a run requires the caller to pass the
    connection in.
    """

    # How often the sweeper wakes up to check. Independent of the per-run
    # idle threshold, which lives in `idle_timeout_s`.
    _SWEEP_INTERVAL_S: float = 30.0

    def __init__(self, idle_timeout_s: float = 600.0) -> None:
        self._runs: dict[str, SubagentRun] = {}
        self.idle_timeout_s = idle_timeout_s
        # Status-change observer (prev, new, detail) — bound on each run
        # via SubagentRun._on_status_change. The serve loop sets this so
        # status flips emit a SubagentStatusChange protocol message.
        self.on_status_change: Callable[
            [SubagentRun, str, str, str | None], Awaitable[None] | None,
        ] | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        # Pending subagent activity events for this dialog. Pushed when a
        # turn ends; drained by the game MCP envelope so the main agent
        # picks them up in `meta.subagent_events` on its next call.
        self._pending_events: list[dict] = []

    def __contains__(self, subagent_id: str) -> bool:
        return subagent_id in self._runs

    def __len__(self) -> int:
        return len(self._runs)

    def get(self, subagent_id: str) -> SubagentRun | None:
        return self._runs.get(subagent_id)

    def list(self) -> list[SubagentRun]:
        """All runs in `created_at` order — what the model sees from
        `subagent_list`."""
        return sorted(self._runs.values(), key=lambda r: r.created_at)

    async def open(
        self,
        spec: AgentSpec,
        conn: AgentConnection,
        cwd: str,
        mcp_servers: list[dict],
    ) -> SubagentRun:
        """Open a new ACP session for `spec` on `conn` and register the run.

        The caller is responsible for actually starting the first turn —
        registry only handles bookkeeping. This split lets `delegate(...)`
        atomically open + prime in one go without exposing intermediate
        state to other tools.
        """
        sid = _make_subagent_id(spec.slug, existing=set(self._runs.keys()))
        scope = list(spec.qualified_allowed_tools())
        # Also accept the normalized `<server>.<tool>` form so glob-style
        # tool allowlists (e.g. `game.find_placement`) match what `request_permission`
        # checks against. See `_tool_match_names`.
        scope.extend(f"game.{t}" for t in spec.allowed_mcp_tools)
        session = await conn.new_session(
            cwd=cwd, mcp_servers=mcp_servers, allowed_tools=scope,
        )
        run = SubagentRun(
            subagent_id=sid,
            spec=spec,
            session=session,
            _on_status_change=self.on_status_change,
        )
        self._runs[sid] = run
        log.info(
            "subagent registered: %s spec=%s session=%s",
            sid, spec.slug, session.session_id,
        )
        return run

    async def close(self, subagent_id: str) -> None:
        """Cancel any in-flight turn, close the ACP session, drop from the registry."""
        run = self._runs.pop(subagent_id, None)
        if run is None:
            return
        if run.turn_task is not None and not run.turn_task.done():
            run.turn_task.cancel()
        try:
            await run.session.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            log.exception("error closing subagent %s session", subagent_id)
        run.set_status("closed")
        # Push a `closed` SubagentEvent so the main agent learns about the
        # disposal via `meta.subagent_events` instead of having to poll
        # `subagent_status` / `subagent_list`. The run is already off
        # `_runs` here, but `push_event` only reads `run.spec` and
        # `run.transcript`, both of which are still readable.
        self.push_event(run, kind="closed")

    async def cancel(self, subagent_id: str) -> SubagentRun | None:
        """Cancel the in-flight turn; keep the session open.

        Returns the run (so the caller can return its updated status) or
        None if the subagent_id is unknown.
        """
        run = self._runs.get(subagent_id)
        if run is None:
            return None
        if run.turn_task is not None and not run.turn_task.done():
            run.turn_task.cancel()
        try:
            await run.session.cancel()
        except Exception:  # noqa: BLE001 - cancel is best-effort
            log.exception("error cancelling subagent %s session", subagent_id)
        # Don't clobber a terminal state — if the turn happened to finish
        # successfully or error out before our cancel landed, that outcome
        # is the truth: the transcript already records the reply (or
        # last_error), and re-stamping "cancelled" would be a lie. Only
        # mark cancelled when the run was still in motion.
        if run.status not in ("completed", "errored"):
            run.set_status("cancelled")
        run.touch()
        return run

    async def close_all(self) -> None:
        """Cancel + close every run. Used on main-handle eviction."""
        for sid in list(self._runs.keys()):
            await self.close(sid)

    # --- subagent events queue (for main-agent meta) ----------------------

    # Reply excerpts are trimmed to keep the envelope small; the agent can
    # fetch the full text via `subagent_transcript`.
    _EXCERPT_CHARS: int = 400

    def push_event(
        self,
        run: SubagentRun,
        kind: str,
        stop_reason: str | None = None,
    ) -> None:
        """Record a subagent turn-end event for later inclusion in
        `meta.subagent_events`.

        Called from the delegation layer's `_drive_turn` when a turn
        reaches a terminal state. Kept on the registry (not the run)
        because the consumer is the *dialog* (which reads through any
        game MCP tool response), not any individual run.
        """
        reply = (run.transcript[-1].agent_reply if run.transcript else "") or ""
        excerpt: str | None = None
        if reply:
            excerpt = reply if len(reply) <= self._EXCERPT_CHARS else reply[: self._EXCERPT_CHARS] + "…"
        self._pending_events.append({
            "subagent_id": run.subagent_id,
            "agent": run.spec.slug,
            "kind": kind,
            "status": run.status,
            "stop_reason": stop_reason or (
                run.transcript[-1].stop_reason if run.transcript else None
            ),
            "reply_excerpt": excerpt,
            "last_error": run.last_error,
            "timestamp": time.monotonic(),
        })

    def drain_events(self) -> list[dict]:
        """Return + clear all pending subagent events. Called per MCP
        tool response by the game envelope builder."""
        events = self._pending_events
        self._pending_events = []
        return events

    # --- idle sweeper ----------------------------------------------------

    def start_idle_sweeper(self) -> None:
        """Spawn the periodic sweeper task. No-op if already running.

        Safe to call from `SubagentBroker.register`; the task is bound to
        the current event loop and lives until `stop_idle_sweeper` is
        called or it gets cancelled by TaskGroup teardown.
        """
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (test / setup). Caller can re-invoke once
            # the loop is up.
            return
        self._sweeper_task = loop.create_task(
            self._idle_sweep_loop(), name="subagent-idle-sweeper",
        )

    async def stop_idle_sweeper(self) -> None:
        """Cancel the sweeper task and wait for it to wind down."""
        task = self._sweeper_task
        self._sweeper_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _idle_sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._SWEEP_INTERVAL_S)
                await self._sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the sweeper alive
            log.exception("subagent idle sweeper crashed; will restart on next register")

    async def _sweep_once(self) -> None:
        """Close every run whose status is sweepable and last_active_at is
        older than the configured timeout. Running turns are never reclaimed.
        """
        cutoff = time.monotonic() - self.idle_timeout_s
        stale = [
            r.subagent_id for r in list(self._runs.values())
            if r.status in _SWEEPABLE and r.last_active_at < cutoff
        ]
        for sid in stale:
            log.info("subagent idle-sweeper closing %s (timeout=%.0fs)", sid, self.idle_timeout_s)
            await self.close(sid)
