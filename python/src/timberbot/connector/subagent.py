"""Subagent registry and lifecycle for `tbot serve`.

A `SubagentRun` is one delegation: a code-defined `AgentSpec`, the ACP
`Session` running it, the transcript of `(user_message, agent_reply)` turns,
and a status state machine (idle → running → completed | errored | cancelled
→ closed).

`SubagentRegistry` is per-user — Timberbot's main `_user_message_loop` keeps
one registry per user_id alongside the user's `AgentConnection`. The
`delegate(...)` MCP tool reads the calling user via `USER_BY_MCP_SESSION`
(see `user_api/serve.py`) and operates on that registry.

Phase 1 covers: open, get, close, list, status state machine, ID collision
retry. Idle-timeout sweeping is deferred to Phase 2 (see
`design/subagent-delegation.md` §6.1).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
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

    @property
    def turns_completed(self) -> int:
        return len(self.transcript)

    def touch(self) -> None:
        self.last_active_at = time.monotonic()


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


class SubagentRegistry:
    """Per-user dict of live `SubagentRun`s.

    Phase 1 keeps the API minimal: open, get, list, close. Phase 2 will add
    an idle sweeper task that closes runs whose `last_active_at` is past the
    configured timeout. The registry doesn't own the `AgentConnection` — the
    serve loop does — so opening a run requires the caller to pass the
    connection in.
    """

    def __init__(self) -> None:
        self._runs: dict[str, SubagentRun] = {}

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
        run = SubagentRun(subagent_id=sid, spec=spec, session=session)
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
        run.status = "closed"

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
        run.status = "cancelled"
        run.touch()
        return run

    async def close_all(self) -> None:
        """Cancel + close every run. Used on main-handle eviction."""
        for sid in list(self._runs.keys()):
            await self.close(sid)
