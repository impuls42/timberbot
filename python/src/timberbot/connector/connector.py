from __future__ import annotations

from timberbot.connector.adapters.base import RuntimeAdapter
from timberbot.connector.session import SessionHandle


class ACPConnector:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._adapter = adapter
        self._allowed_tools: list[str] = allowed_tools or []
        self._cwd = cwd

    async def connect(self, binary: str, model: str) -> SessionHandle:
        argv = self._adapter.build_argv(binary, model)
        handle = SessionHandle(allowed_tools=self._allowed_tools, model=model)
        await handle.start(argv, cwd=self._cwd)
        await handle.initialize()
        return handle
