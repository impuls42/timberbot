"""End-to-end tests for the delegate-family MCP tools.

The tools are registered on a throwaway FastMCP instance with a stubbed
`SubagentBroker` whose `lookup_by_request` returns a fixed UserState — no
HTTP request context needed.

The fake AgentConnection / Session pair lets us drive the prompt flow
synchronously: `prompt_awaitable` returns a canned reply, `prompt` (the
fire-and-forget variant) records the call. That's enough to exercise every
branch of `delegate`, `subagent_reply`, `subagent_status`, `subagent_wait`,
`subagent_cancel`, `subagent_close`, `subagent_list`.
"""
from __future__ import annotations

import asyncio
import contextlib

import fastmcp
import pytest

from timberbot.connector.subagent import SubagentRegistry
from timberbot.game_mcp.delegation import (
    SubagentBroker,
    UserState,
    register_delegation_tools,
)


class _FakeSession:
    """Stand-in for connector.Session.

    `prompt_awaitable(text)` returns the canned `next_reply` (default
    "ok") and records the prompt. Tests can change `next_reply` between
    calls to assert per-turn behavior.
    """

    def __init__(self, conn, session_id: str, allowed_tools):
        self._conn = conn
        self.session_id = session_id
        self.allowed_tools = list(allowed_tools or [])
        self.next_reply = "ok"
        self.stop_reason_to_return = "end_turn"
        self.calls: list[str] = []
        self.closed = False
        self.cancelled = False
        self._busy = False
        self._block = asyncio.Event()  # used in tests that want to hold a turn open
        self._block.set()  # default: don't block

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def current_stop_reason(self) -> str | None:
        return self.stop_reason_to_return

    async def prompt_awaitable(self, text: str) -> str:
        self._busy = True
        self.calls.append(text)
        try:
            await self._block.wait()
        finally:
            self._busy = False
        return self.next_reply

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True
        self._conn._sessions.pop(self.session_id, None)


class _FakeAgentConnection:
    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {}
        self._counter = 0

    async def new_session(self, cwd, mcp_servers, allowed_tools=None):
        self._counter += 1
        sid = f"sess-{self._counter}"
        s = _FakeSession(self, sid, allowed_tools)
        self._sessions[sid] = s
        return s


@pytest.fixture
def harness() -> tuple[fastmcp.FastMCP, SubagentBroker, _FakeAgentConnection, SubagentRegistry]:
    """A FastMCP server with delegation tools registered and a stubbed broker
    whose lookup_by_request returns a fixed `UserState`."""
    mcp = fastmcp.FastMCP("test-delegate")
    broker = SubagentBroker()
    conn = _FakeAgentConnection()
    registry = SubagentRegistry()
    broker._users["u1"] = UserState(
        conn=conn, registry=registry, agent_cwd="/tmp", mcp_servers=[],
    )
    # Stub the HTTP lookup so we don't need a real Starlette request.
    broker.lookup_by_request = lambda: broker._users["u1"]  # type: ignore[assignment]
    register_delegation_tools(mcp, broker)
    return mcp, broker, conn, registry


async def _call(mcp: fastmcp.FastMCP, tool: str, **kwargs) -> dict:
    result = await mcp.call_tool(tool, arguments=kwargs)
    return result.structured_content


# --- delegate -----------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_unknown_agent_returns_error(harness):
    mcp, *_ = harness
    res = await _call(mcp, "delegate", agent="ghost", task="hi")
    assert "error" in res
    assert "scout" in res["available"]


@pytest.mark.asyncio
async def test_delegate_wait_false_returns_running_immediately(harness):
    mcp, _, conn, registry = harness
    res = await _call(mcp, "delegate", agent="scout", task="find spot")
    assert res["status"] == "running"
    sid = res["subagent_id"]
    assert sid.startswith("scout-")
    # The run is registered; turn task is scheduled but we haven't awaited it.
    run = registry.get(sid)
    assert run is not None
    assert run.turn_task is not None
    # Let the turn complete.
    await run.turn_task
    assert run.status == "completed"
    assert run.transcript and run.transcript[0].user_message == "find spot"
    # The actual session.prompt_awaitable saw the bootstrap-prefixed prompt.
    primed = run.session.calls[0]
    assert "SUBAGENT_SYSTEM_BOUNDARY" in primed
    assert primed.endswith("find spot")


@pytest.mark.asyncio
async def test_delegate_wait_true_returns_reply(harness):
    mcp, _, conn, registry = harness
    res = await _call(mcp, "delegate", agent="scout", task="find spot", wait=True)
    assert res["status"] == "completed"
    assert res["stop_reason"] == "end_turn"
    assert res["reply"] == "ok"


@pytest.mark.asyncio
async def test_delegate_no_user_bound_returns_error(harness):
    mcp, broker, *_ = harness
    broker.lookup_by_request = lambda: None  # type: ignore[assignment]
    res = await _call(mcp, "delegate", agent="scout", task="hi")
    assert "error" in res
    assert "no Timberbot user" in res["error"]


# --- subagent_reply -----------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_reply_advances_transcript(harness):
    mcp, _, _, registry = harness
    opened = await _call(mcp, "delegate", agent="scout", task="initial", wait=True)
    sid = opened["subagent_id"]

    res = await _call(mcp, "subagent_reply", subagent_id=sid, message="follow-up", wait=True)
    assert res["status"] == "completed"

    run = registry.get(sid)
    assert run is not None
    assert [t.user_message for t in run.transcript] == ["initial", "follow-up"]


@pytest.mark.asyncio
async def test_subagent_reply_unknown_id(harness):
    mcp, *_ = harness
    res = await _call(mcp, "subagent_reply", subagent_id="ghost-0000", message="x")
    assert "error" in res
    assert "unknown subagent_id" in res["error"]


@pytest.mark.asyncio
async def test_subagent_reply_when_busy_rejects(harness):
    mcp, _, conn, registry = harness
    # delegate + hold the turn open.
    opened = await _call(mcp, "delegate", agent="scout", task="initial")
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None
    run.session._block.clear()
    # The turn is in flight; reply should be rejected.
    res = await _call(mcp, "subagent_reply", subagent_id=sid, message="x")
    assert res.get("error") == "busy"
    # Cleanup: release and wait.
    run.session._block.set()
    await run.turn_task


@pytest.mark.asyncio
async def test_subagent_reply_rejects_before_background_turn_starts(harness):
    """Regression: between `delegate(wait=False)` scheduling `_drive_turn`
    and that coroutine actually executing, `session.is_busy` is still False
    (the wrapped `prompt_awaitable` hasn't run). A rapid `subagent_reply`
    must still be rejected — the busy check has to look at `run.turn_task`
    too, not only `session.is_busy`.

    Driven via a stub task on a freshly-registered run rather than racing
    real timing, so the test is deterministic.
    """
    mcp, _, _, registry = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t", wait=True)
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None
    # Drop the completed turn_task and attach a never-resolving one to
    # simulate "scheduled but not yet running" — exactly the state where
    # session.is_busy is False but a follow-up reply must still bounce.
    parked: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    run.turn_task = asyncio.ensure_future(parked)
    run.status = "running"
    assert run.session.is_busy is False  # the new guard's whole point
    assert run.turn_task is not None and not run.turn_task.done()

    res = await _call(mcp, "subagent_reply", subagent_id=sid, message="follow-up")
    assert res.get("error") == "busy"
    # Status must not have been polluted to "errored" by a stray
    # prompt_awaitable busy raise inside _drive_turn.
    assert run.status == "running"

    # Cleanup so the never-resolving task doesn't leak across tests.
    run.turn_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run.turn_task


# --- subagent_status / subagent_list ------------------------------------


@pytest.mark.asyncio
async def test_subagent_status_returns_metadata(harness):
    mcp, *_ = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t", wait=True)
    sid = opened["subagent_id"]
    res = await _call(mcp, "subagent_status", subagent_id=sid)
    assert res["agent"] == "scout"
    assert res["status"] == "completed"
    assert res["turns_completed"] == 1
    # status payload deliberately omits reply text.
    assert "reply" not in res


@pytest.mark.asyncio
async def test_subagent_list_includes_every_run(harness):
    mcp, *_ = harness
    a = await _call(mcp, "delegate", agent="scout", task="a", wait=True)
    b = await _call(mcp, "delegate", agent="wirer", task="b", wait=True)
    res = await _call(mcp, "subagent_list")
    ids = [s["subagent_id"] for s in res["subagents"]]
    assert a["subagent_id"] in ids
    assert b["subagent_id"] in ids


# --- subagent_wait ------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_wait_returns_reply_after_turn_ends(harness):
    mcp, _, _, registry = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t")
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None
    # Hold the turn briefly then release.
    run.session._block.clear()
    async def release_soon():
        await asyncio.sleep(0.05)
        run.session._block.set()
    asyncio.create_task(release_soon())
    res = await _call(mcp, "subagent_wait", subagent_id=sid, timeout=2.0)
    assert res["status"] == "completed"
    assert res["reply"] == "ok"
    assert res["timed_out"] is False


@pytest.mark.asyncio
async def test_subagent_wait_times_out(harness):
    mcp, _, _, registry = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t")
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None
    run.session._block.clear()
    res = await _call(mcp, "subagent_wait", subagent_id=sid, timeout=0.05)
    assert res["timed_out"] is True
    assert res["status"] == "running"
    # Cleanup.
    run.session._block.set()
    await run.turn_task


@pytest.mark.asyncio
async def test_subagent_wait_on_already_done_returns_immediately(harness):
    mcp, *_ = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t", wait=True)
    sid = opened["subagent_id"]
    res = await _call(mcp, "subagent_wait", subagent_id=sid)
    assert res["reply"] == "ok"
    assert res["status"] == "completed"


# --- subagent_cancel / subagent_close -----------------------------------


@pytest.mark.asyncio
async def test_subagent_cancel_keeps_session_alive(harness):
    mcp, _, _, registry = harness
    # Open the run and hold the first turn parked so cancel observes a
    # non-terminal state. Cancelling an already-`completed` run is a no-op
    # by design (see test below).
    opened = await _call(mcp, "delegate", agent="scout", task="t")
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None
    run.session._block.clear()  # park _drive_turn inside prompt_awaitable

    res = await _call(mcp, "subagent_cancel", subagent_id=sid)
    assert res["status"] == "cancelled"
    # Still listed — cancel keeps the session open for follow-ups.
    listing = await _call(mcp, "subagent_list")
    assert sid in {s["subagent_id"] for s in listing["subagents"]}


@pytest.mark.asyncio
async def test_subagent_cancel_on_completed_run_does_not_clobber_status(harness):
    """If the background turn happened to finish successfully between when
    cancel was requested and when it lands, the registry must not overwrite
    `completed` with `cancelled` — the transcript already records the
    truthful outcome."""
    mcp, _, _, registry = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t", wait=True)
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None and run.status == "completed"

    res = await _call(mcp, "subagent_cancel", subagent_id=sid)
    # Status stays "completed"; the run keeps its terminal state.
    assert res["status"] == "completed"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_subagent_close_drops_from_list(harness):
    mcp, *_ = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t", wait=True)
    sid = opened["subagent_id"]
    res = await _call(mcp, "subagent_close", subagent_id=sid)
    assert res["ok"] is True
    listing = await _call(mcp, "subagent_list")
    assert sid not in {s["subagent_id"] for s in listing["subagents"]}


@pytest.mark.asyncio
async def test_subagent_close_unknown_id(harness):
    mcp, *_ = harness
    res = await _call(mcp, "subagent_close", subagent_id="ghost-0000")
    assert "error" in res


@pytest.mark.asyncio
async def test_subagent_cancel_does_not_log_cancellederror(harness, caplog):
    """Fire-and-forget background turn cancellation should be silent —
    `_drain_background_turn` swallows the CancelledError so asyncio's default
    handler doesn't log it as an unhandled exception."""
    import logging
    mcp, _, _, registry = harness
    opened = await _call(mcp, "delegate", agent="scout", task="t")
    sid = opened["subagent_id"]
    run = registry.get(sid)
    assert run is not None
    run.session._block.clear()  # hold the turn open
    with caplog.at_level(logging.WARNING):
        await _call(mcp, "subagent_cancel", subagent_id=sid)
        # Give the cancellation a chance to fully unwind.
        await asyncio.sleep(0.05)
    assert "background subagent turn failed" not in caplog.text


# --- broker → user_id routing ------------------------------------------


@pytest.mark.asyncio
async def test_broker_register_then_lookup_by_id():
    """The broker.register / get pair is the surface `tbot serve` calls."""
    broker = SubagentBroker()
    conn = _FakeAgentConnection()
    reg = broker.register(
        user_id="alice", conn=conn, agent_cwd="/tmp", mcp_servers=[],
    )
    assert isinstance(reg, SubagentRegistry)
    state = broker.get("alice")
    assert state is not None and state.conn is conn

    await broker.unregister("alice")
    assert broker.get("alice") is None
