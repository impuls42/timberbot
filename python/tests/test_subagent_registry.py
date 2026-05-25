"""Tests for `connector.subagent.SubagentRegistry` and ID generation."""
from __future__ import annotations

import pytest

from timberbot.connector.agent_spec import (
    AUDITOR_SPEC,
    SCOUT_SPEC,
    WIRER_SPEC,
)
from timberbot.connector.subagent import (
    SubagentRegistry,
    SubagentRun,
    _make_subagent_id,
)


class _FakeSession:
    """Stand-in for connector.Session — close() drops the id from the conn's _sessions."""

    def __init__(self, conn, session_id: str, allowed_tools):
        self._conn = conn
        self.session_id = session_id
        self.allowed_tools = allowed_tools
        self.closed = False
        self.cancelled = False

    @property
    def is_busy(self) -> bool:
        return False

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True
        self._conn._sessions.pop(self.session_id, None)


class _FakeAgentConnection:
    """Stand-in for connector.AgentConnection — `new_session` mints _FakeSession objects."""

    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSession] = {}
        self._counter = 0
        self.new_session_calls: list[tuple[str, list[dict], list[str] | None]] = []

    async def new_session(self, cwd, mcp_servers, allowed_tools=None):
        self._counter += 1
        sid = f"sess-{self._counter}"
        self.new_session_calls.append((cwd, mcp_servers, list(allowed_tools or [])))
        s = _FakeSession(self, sid, allowed_tools)
        self._sessions[sid] = s
        return s


# --- ID generation ------------------------------------------------------


def test_make_subagent_id_format():
    sid = _make_subagent_id("scout", existing=set())
    assert sid.startswith("scout-")
    nonce = sid.split("-", 1)[1]
    assert len(nonce) == 4
    int(nonce, 16)  # raises if non-hex


def test_make_subagent_id_collision_retry():
    # Pre-fill existing with every possible single-byte nonce — collision is
    # guaranteed in the 4-hex space until secrets falls into a free slot.
    # Use a small simulated collision instead.
    existing: set[str] = {"scout-aaaa"}
    sid = _make_subagent_id("scout", existing=existing)
    assert sid != "scout-aaaa"


def test_make_subagent_id_exhausts_retries():
    """If every nonce attempted is already taken, raise. Realistically
    impossible at 65k IDs/slug, but the safeguard is contractually required."""
    # Monkeypatch `secrets.token_hex` for this test so collisions are forced.
    import secrets as _secrets

    from timberbot.connector import subagent as _sa
    orig = _sa.secrets

    class _FixedSecrets:
        @staticmethod
        def token_hex(n):
            return "dead"

    _sa.secrets = _FixedSecrets()  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="collision"):
            _make_subagent_id("scout", existing={"scout-dead"}, retries=3)
    finally:
        _sa.secrets = orig


# --- registry lifecycle -------------------------------------------------


@pytest.mark.asyncio
async def test_open_registers_and_returns_run():
    reg = SubagentRegistry()
    conn = _FakeAgentConnection()
    run = await reg.open(SCOUT_SPEC, conn, cwd="/tmp", mcp_servers=[])

    assert isinstance(run, SubagentRun)
    assert run.subagent_id.startswith("scout-")
    assert run.spec is SCOUT_SPEC
    assert run.status == "idle"
    assert run.session.session_id in conn._sessions
    assert run.subagent_id in reg


@pytest.mark.asyncio
async def test_open_passes_scoped_allowed_tools():
    """Subagent gets its spec's allowed tools — not the main agent's."""
    reg = SubagentRegistry()
    conn = _FakeAgentConnection()
    await reg.open(SCOUT_SPEC, conn, cwd="/tmp", mcp_servers=[])

    _, _, allowed = conn.new_session_calls[0]
    # Both the full mcp__game__ form and the normalized game.* form must be
    # included so glob-style permission matching works.
    assert "mcp__game__find_placement" in allowed
    assert "game.find_placement" in allowed
    # Scout is read-only — place_building must NOT be allowed.
    assert not any("place_building" in t for t in allowed)


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_id():
    reg = SubagentRegistry()
    assert reg.get("scout-deadbeef") is None


@pytest.mark.asyncio
async def test_list_returns_runs_in_creation_order():
    reg = SubagentRegistry()
    conn = _FakeAgentConnection()
    a = await reg.open(SCOUT_SPEC, conn, cwd="/tmp", mcp_servers=[])
    b = await reg.open(WIRER_SPEC, conn, cwd="/tmp", mcp_servers=[])
    c = await reg.open(AUDITOR_SPEC, conn, cwd="/tmp", mcp_servers=[])
    ids = [r.subagent_id for r in reg.list()]
    assert ids == [a.subagent_id, b.subagent_id, c.subagent_id]


@pytest.mark.asyncio
async def test_close_removes_from_registry_and_closes_session():
    reg = SubagentRegistry()
    conn = _FakeAgentConnection()
    run = await reg.open(SCOUT_SPEC, conn, cwd="/tmp", mcp_servers=[])
    sid = run.subagent_id
    sess = run.session

    await reg.close(sid)
    assert sid not in reg
    assert sess.closed is True
    assert run.status == "closed"


@pytest.mark.asyncio
async def test_cancel_keeps_session_open_for_followups():
    reg = SubagentRegistry()
    conn = _FakeAgentConnection()
    run = await reg.open(SCOUT_SPEC, conn, cwd="/tmp", mcp_servers=[])
    sid = run.subagent_id

    cancelled = await reg.cancel(sid)
    assert cancelled is run
    assert run.status == "cancelled"
    assert run.session.cancelled is True
    # Cancelled runs stay in the registry until explicitly closed.
    assert sid in reg


@pytest.mark.asyncio
async def test_cancel_unknown_id_returns_none():
    reg = SubagentRegistry()
    assert (await reg.cancel("scout-deadbeef")) is None


@pytest.mark.asyncio
async def test_close_all_drops_every_run():
    reg = SubagentRegistry()
    conn = _FakeAgentConnection()
    await reg.open(SCOUT_SPEC, conn, cwd="/tmp", mcp_servers=[])
    await reg.open(WIRER_SPEC, conn, cwd="/tmp", mcp_servers=[])
    await reg.open(AUDITOR_SPEC, conn, cwd="/tmp", mcp_servers=[])

    await reg.close_all()
    assert len(reg) == 0
    # All sessions on the connection should have been closed too.
    assert all(s.closed for s in conn._sessions.values()) if conn._sessions else True
