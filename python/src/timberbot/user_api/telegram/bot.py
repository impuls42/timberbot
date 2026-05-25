from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from timberbot.user_api.protocol import (
    AgentFeedback,
    ConnectorMessage,
    GameElicitation,
    SessionStateChange,
    SubagentStatusChange,
    TextChunk,
    ToolAction,
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
        # Fallback routing: the user's most recent chat, captured from every
        # inbound message. Lets us reply to /status, /cancel, /state, etc.
        # before any ACP session exists (i.e. before register_chat has fired).
        self._chat_by_user: dict[str, int] = {}
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
        elif isinstance(msg, AgentFeedback):
            await self._handle_feedback(msg)
        elif isinstance(msg, ToolAction):
            await self._handle_tool_action(msg)
        elif isinstance(msg, SubagentStatusChange):
            await self._handle_subagent_status(msg)

    def _resolve_chat(self, session_id: str, user_id: str | None) -> int | None:
        """Resolve an outgoing message's target chat.

        Priority: explicit session-id binding (set when an ACP session goes
        active), then the originating user's most recent chat (set on every
        inbound message). The second branch is what lets us reply to /status
        or /state before any agent session exists.
        """
        chat_id = self._chat_ids.get(session_id)
        if chat_id is not None:
            return chat_id
        if user_id is not None:
            return self._chat_by_user.get(user_id)
        return None

    async def _handle_text_chunk(self, msg: TextChunk) -> None:
        buf = self._buffers.get(msg.session_id)
        if buf is None:
            chat_id = self._resolve_chat(msg.session_id, msg.user_id)
            if chat_id is None:
                log.warning("No chat_id for session %s; dropping chunk", msg.session_id)
                return
            placeholder = await self._app.bot.send_message(chat_id=chat_id, text="…")
            buf = StreamBuffer(chat_id=chat_id, bot=self._app.bot, message_id=placeholder.message_id)
            self._buffers[msg.session_id] = buf
            await buf.start()
        buf.feed(msg.text)

    async def _handle_elicitation(self, msg: GameElicitation) -> None:
        chat_id = self._resolve_chat(msg.session_id, msg.user_id)
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
        chat_id = self._resolve_chat(msg.session_id, msg.user_id)
        if chat_id is None:
            log.warning("No chat_id for session %s; dropping state change", msg.session_id)
            return
        # "info" carries a free-form body (e.g. /state output); render the
        # detail directly. Everything else gets the "Session X: detail" shell.
        if msg.state == "info" and msg.detail:
            text = msg.detail
        else:
            detail = f": {msg.detail}" if msg.detail else ""
            text = f"Session {msg.state}{detail}"
        await self._app.bot.send_message(chat_id=chat_id, text=text)

        if msg.state == "ended":
            buf = self._buffers.pop(msg.session_id, None)
            if buf is not None:
                await buf.stop()

    async def _handle_feedback(self, msg: AgentFeedback) -> None:
        text = f"[feedback/{msg.category}/{msg.severity}] {msg.message}"
        # If the feedback names a specific originating user, target that
        # user's most recent chat. Otherwise fall back to broadcasting to
        # every chat we've ever bound a session to — matches the historical
        # behavior from #77 when no user_id was wired.
        if msg.user_id is not None:
            chat_id = self._chat_by_user.get(msg.user_id)
            targets: set[int] = {chat_id} if chat_id is not None else set()
        else:
            targets = set(self._chat_ids.values())
        for chat_id in targets:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                log.warning("Failed to deliver feedback notification to chat_id %s", chat_id)

    async def _handle_tool_action(self, msg: ToolAction) -> None:
        """Render a tool-action notification as a fresh chat message.

        Distinct from `_handle_text_chunk` because these aren't part of the
        agent's streaming reply — they're standalone "the bot did X" events
        that should land as their own message between turns, not be edited
        into the running stream buffer. Subagent actions get a
        `[<subagent_id>] …` prefix so the user can tell which agent ran.
        """
        chat_id = self._resolve_chat(msg.session_id, msg.user_id)
        if chat_id is None:
            log.warning("No chat_id for session %s; dropping tool action", msg.session_id)
            return
        text = (
            f"[{msg.subagent_id}] {msg.summary}" if msg.subagent_id else msg.summary
        )
        await self._app.bot.send_message(chat_id=chat_id, text=text)

    async def _handle_subagent_status(self, msg: SubagentStatusChange) -> None:
        """One concise line per subagent status transition.

        Skips the noisy `idle → running` / `running → idle` flips and
        surfaces the ones that signal real progress (`completed`, `errored`,
        `cancelled`, `closed`). The user gets a fan-out view without seeing
        every internal scheduler tick.
        """
        # Filter out the not-very-informative transitions. Status-change
        # observers fire on every flip; we only want to talk about terminal
        # states.
        if msg.new_status not in ("completed", "errored", "cancelled", "closed"):
            return
        chat_id = self._chat_by_user.get(msg.user_id)
        if chat_id is None:
            log.debug("no chat_id for user %s; dropping subagent status", msg.user_id)
            return
        verb = msg.new_status
        text = f"[{msg.subagent_id}] {verb}"
        if msg.detail:
            text = f"{text}: {msg.detail}"
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.warning("Failed to deliver subagent status to chat_id %s", chat_id)

    def reset_stream(self, session_id: str) -> None:
        """Drop the streaming buffer for a session so the next chunk starts
        fresh.

        Called between user turns: continuing to edit the placeholder from
        the previous turn (which now sits above the user's newest message in
        the chat) is confusing. After reset, the next `TextChunk` opens a new
        placeholder below the user's most recent message.
        """
        buf = self._buffers.pop(session_id, None)
        if buf is not None:
            asyncio.create_task(buf.stop())

    def register_chat(self, session_id: str, chat_id: int) -> None:
        self._chat_ids[session_id] = chat_id

    async def messages(self) -> AsyncIterator[UserMessage]:  # type: ignore[override]
        while True:
            m = await self._queue.get()
            if m.chat_id is not None:
                self._chat_by_user[m.user_id] = m.chat_id
            yield m

    async def start(self) -> None:
        handlers = make_handlers(self._queue, self._allowed_users)
        cmd_filter = filters.User(user_id=list(self._allowed_users)) if self._allowed_users else None
        for name in ("prompt", "cancel", "halt", "status", "state"):
            self._app.add_handler(CommandHandler(name, handlers[name], filters=cmd_filter))
        # Plain text (anything not starting with `/`) is forwarded as a prompt.
        # Same allowlist filter so non-listed users can't slip past via plain
        # text. Registered after command handlers so /prompt /state etc. still
        # take priority for slash messages.
        text_filter = filters.TEXT & ~filters.COMMAND
        if cmd_filter is not None:
            text_filter = text_filter & cmd_filter
        self._app.add_handler(MessageHandler(text_filter, handlers["text"]))
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
