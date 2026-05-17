from __future__ import annotations

import asyncio
import contextlib
import json
import logging

log = logging.getLogger("timberbot.connector")


class SubprocessTransport:
    def __init__(self, argv: list[str], cwd: str | None = None, env: dict | None = None) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
        )
        self._stderr_task = asyncio.get_running_loop().create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            log.warning("agent stderr: %s", line.decode(errors="replace").rstrip())

    async def send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        data = json.dumps(msg) + "\n"
        self._proc.stdin.write(data.encode())
        await self._proc.stdin.drain()

    async def recv_line(self) -> dict | None:
        assert self._proc and self._proc.stdout
        try:
            line = await self._proc.stdout.readline()
        except Exception as exc:
            log.warning("recv_line stdout read failed: %s", exc)
            return None
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("recv_line dropping non-JSON frame (%s): %r", exc, line[:200])
            return None

    async def close(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        if self._proc:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
