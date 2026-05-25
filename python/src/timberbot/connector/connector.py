from __future__ import annotations

from timberbot.connector.adapters.base import RuntimeAdapter
from timberbot.connector.session import AgentConnection


class ACPConnector:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._adapter = adapter
        self._allowed_tools: list[str] = list(allowed_tools or [])
        self._cwd = cwd

    @property
    def allowed_tools(self) -> list[str]:
        """Default per-session tool scope. Callers pass this when opening
        sessions that should share the main chat's allowlist; subagent
        sessions override via `AgentConnection.new_session(allowed_tools=...)`.
        """
        return list(self._allowed_tools)

    async def connect(self, binary: str, model: str) -> AgentConnection:
        """Spawn one agent subprocess and return its `AgentConnection`.

        The connection has no sessions yet — call `conn.new_session(...)` to
        open the main chat session, and again for each subagent.
        """
        argv = self._adapter.build_argv(binary, model)
        conn = AgentConnection(model=model)
        await conn.start(argv, cwd=self._cwd)
        await conn.initialize()
        return conn
