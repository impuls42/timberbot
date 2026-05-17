from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("timberbot.user_api")


class StreamBuffer:
    CHAR_THRESHOLD = 500
    FLUSH_INTERVAL = 0.5  # seconds

    def __init__(self, chat_id: int, bot: object, message_id: int | None = None) -> None:
        self._chat_id = chat_id
        self._bot = bot
        self._message_id = message_id
        self._buffer: list[str] = []
        self._since_flush: int = 0
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    def feed(self, chunk: str) -> None:
        self._buffer.append(chunk)
        self._since_flush += len(chunk)
        if self._since_flush >= self.CHAR_THRESHOLD:
            asyncio.create_task(self.flush())

    async def flush(self) -> None:
        if self._message_id is None:
            return
        text = "".join(self._buffer)
        if not text:
            return
        try:
            await self._bot.edit_message_text(  # type: ignore[union-attr]
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
            )
        except Exception:
            log.exception("Failed to edit message %s in chat %s", self._message_id, self._chat_id)
        self._since_flush = 0

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self.flush()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._periodic())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()
