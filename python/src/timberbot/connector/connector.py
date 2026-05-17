from __future__ import annotations

import asyncio

from timberbot.connector.session import SessionHandle
from timberbot.connector.transport import SubprocessTransport


class ACPConnector:
    def __init__(
        self,
        adapter: object,
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._adapter = adapter
        self._allowed_tools: list[str] = allowed_tools or []
        self._cwd = cwd

    async def connect(self, binary: str, model: str) -> SessionHandle:
        argv = self._adapter.build_argv(binary, model)
        transport = SubprocessTransport(argv, cwd=self._cwd)
        await transport.start()

        handle = SessionHandle(transport, self._allowed_tools)
        asyncio.get_running_loop().create_task(handle.read_loop())

        await handle.initialize()
        return handle
