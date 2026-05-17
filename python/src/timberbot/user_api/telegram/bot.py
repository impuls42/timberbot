from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, filters

from timberbot.user_api.protocol import (
    ConnectorMessage,
    GameElicitation,
    SessionStateChange,
    TextChunk,
    UserMessage,
)
from timberbot.user_api.telegram.handlers import make_handlers
from timberbot.user_api.telegram.keyboards import elicitation_keyboard
from timberbot.user_api.telegram.streaming import StreamBuffer

log = logging.getLogger("timberbot.user_api")


class TelegramAdapter:
    def __init__(self, token: str, allowed_users: list[int] | None = None) -> None:
        self._app = Application.builder().token(token).build()
        self._queue: asyncio.Queue[UserMessage] = asyncio.Queue()
        self._buffers: dict[str, StreamBuffer] = {}  # keyed by session_id
        self._chat_ids: dict[str, int] = {}  # session_id -> chat_id
        self._allowed_users: set[int] = set(allowed_users or [])
        if not self._allowed_users:
            log.warning(
                "TelegramAdapter: no allowed_users configured — any Telegram user "
                "who finds this bot can /prompt it. Add `[serve.telegram] "
                "allowed_users = [<your-telegram-user-id>]` to restrict access."
            )

    async def send(self, msg: ConnectorMessage) -> None:
        if isinstance(msg, TextChunk):
            await self._handle_text_chunk(msg)
        elif isinstance(msg, GameElicitation):
            await self._handle_elicitation(msg)
        elif isinstance(msg, SessionStateChange):
            await self._handle_state_change(msg)

    async def _handle_text_chunk(self, msg: TextChunk) -> None:
        buf = self._buffers.get(msg.session_id)
        if buf is None:
            chat_id = self._chat_ids.get(msg.session_id)
            if chat_id is None:
                log.warning("No chat_id for session %s; dropping chunk", msg.session_id)
                return
            placeholder = await self._app.bot.send_message(chat_id=chat_id, text="…")
            buf = StreamBuffer(chat_id=chat_id, bot=self._app.bot, message_id=placeholder.message_id)
            self._buffers[msg.session_id] = buf
            await buf.start()
        buf.feed(msg.text)

    async def _handle_elicitation(self, msg: GameElicitation) -> None:
        chat_id = self._chat_ids.get(msg.session_id)
        if chat_id is None:
            log.warning("No chat_id for session %s; dropping elicitation", msg.session_id)
            return
        keyboard = elicitation_keyboard(msg.choices, msg.correlation_id)
        await self._app.bot.send_message(
            chat_id=chat_id,
            text=msg.question,
            reply_markup=keyboard,
        )

    async def _handle_state_change(self, msg: SessionStateChange) -> None:
        chat_id = self._chat_ids.get(msg.session_id)
        if chat_id is None:
            log.warning("No chat_id for session %s; dropping state change", msg.session_id)
            return
        detail = f": {msg.detail}" if msg.detail else ""
        text = f"Session {msg.state}{detail}"
        await self._app.bot.send_message(chat_id=chat_id, text=text)

        if msg.state == "ended":
            buf = self._buffers.pop(msg.session_id, None)
            if buf is not None:
                await buf.stop()

    def register_chat(self, session_id: str, chat_id: int) -> None:
        self._chat_ids[session_id] = chat_id

    async def messages(self) -> AsyncIterator[UserMessage]:  # type: ignore[override]
        while True:
            yield await self._queue.get()

    async def start(self) -> None:
        handlers = make_handlers(self._queue, self._allowed_users)
        cmd_filter = filters.User(user_id=list(self._allowed_users)) if self._allowed_users else None
        for name in ("prompt", "cancel", "halt", "status"):
            self._app.add_handler(CommandHandler(name, handlers[name], filters=cmd_filter))
        self._app.add_handler(CallbackQueryHandler(handlers["choice_callback"]))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()  # type: ignore[union-attr]

    async def stop(self) -> None:
        await asyncio.gather(*(buf.stop() for buf in self._buffers.values()))
        self._buffers.clear()

        if self._app.updater is not None:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
