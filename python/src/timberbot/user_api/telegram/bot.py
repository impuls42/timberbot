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


def _stream_key(session_id: str, subagent_id: str | None) -> str:
    """Distinguish a subagent's stream buffer from the main agent's.

    Subagent sessions share a single ACP connection but have their own
    session_ids, so `session_id` alone is unique in practice. Composing
    with `subagent_id` makes the keying intent obvious in the code, and
    lets `reset_stream(session_id)` continue to operate per-session
    without accidentally hitting subagent buffers.
    """
    return f"{session_id}#{subagent_id}" if subagent_id else session_id


class TelegramAdapter:
    def __init__(self, token: str, allowed_dialogs: list[int] | None = None) -> None:
        self._app = Application.builder().token(token).build()
        self._queue: asyncio.Queue[UserMessage] = asyncio.Queue()
        # Streaming-text buffers, keyed by `_stream_key`. Each main-agent
        # session gets one; each subagent gets its own under the same
        # session_id so `[<subagent_id>] …` placeholders don't collide
        # with the main reply stream.
        self._buffers: dict[str, StreamBuffer] = {}
        # Maps ACP main-session_id → chat_id, populated by `register_chat`
        # when the loop opens a new main session. Subagent sessions never
        # register here; they route via the message's `dialog_id` instead.
        self._chat_ids: dict[str, int] = {}
        self._allowed_dialogs: set[int] = set(allowed_dialogs or [])
        if not self._allowed_dialogs:
            log.warning(
                "TelegramAdapter: no allowed_dialogs configured — any chat that "
                "discovers this bot can /prompt it. Add `[serve.telegram] "
                "allowed_dialogs = [<your-telegram-chat-id>]` to restrict access."
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

    def _resolve_chat(self, session_id: str, dialog_id: str | None) -> int | None:
        """Resolve an outgoing message's target chat.

        Priority: explicit session-id binding (set on main-session open),
        then the message's `dialog_id` parsed as a chat_id. The second
        branch is what makes the dialog_id rename work — every incoming
        Telegram message carries its chat_id, so any outbound message
        targeting that same dialog can resolve without a fallback table.
        """
        chat_id = self._chat_ids.get(session_id)
        if chat_id is not None:
            return chat_id
        if dialog_id is not None:
            try:
                return int(dialog_id)
            except ValueError:
                return None
        return None

    async def _handle_text_chunk(self, msg: TextChunk) -> None:
        key = _stream_key(msg.session_id, msg.subagent_id)
        buf = self._buffers.get(key)
        if buf is None:
            chat_id = self._resolve_chat(msg.session_id, msg.dialog_id)
            if chat_id is None:
                log.warning("No chat_id for session %s; dropping chunk", msg.session_id)
                return
            # Subagent chunks open their own buffer under a `[<id>] …`
            # placeholder so the user sees one stream per subagent rather
            # than mixed text in the main reply.
            placeholder_text = f"[{msg.subagent_id}] …" if msg.subagent_id else "…"
            placeholder = await self._app.bot.send_message(chat_id=chat_id, text=placeholder_text)
            buf = StreamBuffer(chat_id=chat_id, bot=self._app.bot, message_id=placeholder.message_id)
            self._buffers[key] = buf
            await buf.start()
            # For subagents, prime the buffer with the prefix so subsequent
            # chunks land under the same header.
            if msg.subagent_id:
                buf.feed(f"[{msg.subagent_id}] ")
        buf.feed(msg.text)

    async def _handle_elicitation(self, msg: GameElicitation) -> None:
        chat_id = self._resolve_chat(msg.session_id, msg.dialog_id)
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
        chat_id = self._resolve_chat(msg.session_id, msg.dialog_id)
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
            # Close every stream buffer (main + subagent) that belongs to
            # this session_id.
            for key in [k for k in self._buffers if k == msg.session_id or k.startswith(f"{msg.session_id}#")]:
                buf = self._buffers.pop(key, None)
                if buf is not None:
                    await buf.stop()

    async def _handle_feedback(self, msg: AgentFeedback) -> None:
        text = f"[feedback/{msg.category}/{msg.severity}] {msg.message}"
        # If the feedback names a specific originating dialog, target it
        # directly. Otherwise fall back to broadcasting to every chat we've
        # bound a session to — matches the historical behavior from #77
        # when no dialog_id was wired.
        if msg.dialog_id is not None:
            try:
                targets: set[int] = {int(msg.dialog_id)}
            except ValueError:
                targets = set()
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
        chat_id = self._resolve_chat(msg.session_id, msg.dialog_id)
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
        try:
            chat_id = int(msg.dialog_id)
        except (TypeError, ValueError):
            log.debug("invalid dialog_id %r; dropping subagent status", msg.dialog_id)
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
        """Drop the streaming buffers for a session so the next chunk
        starts fresh.

        Called between user turns: continuing to edit the placeholder from
        the previous turn (which now sits above the user's newest message
        in the chat) is confusing. Drops the main-session buffer AND every
        subagent buffer that opened under it.
        """
        for key in [k for k in self._buffers if k == session_id or k.startswith(f"{session_id}#")]:
            buf = self._buffers.pop(key, None)
            if buf is not None:
                asyncio.create_task(buf.stop())

    def register_chat(self, session_id: str, chat_id: int) -> None:
        self._chat_ids[session_id] = chat_id

    async def messages(self) -> AsyncIterator[UserMessage]:  # type: ignore[override]
        while True:
            m = await self._queue.get()
            yield m

    async def start(self) -> None:
        handlers = make_handlers(self._queue, self._allowed_dialogs)
        # Filter on chat id, not user id — the bot accepts any user posting
        # into an allowed chat. (Group-chat support comes for free with this
        # framing; if it turns out we want stricter per-user gating again,
        # we can add a separate `allowed_users` knob later.)
        cmd_filter = filters.Chat(chat_id=list(self._allowed_dialogs)) if self._allowed_dialogs else None
        for name in ("prompt", "cancel", "halt", "status", "state"):
            self._app.add_handler(CommandHandler(name, handlers[name], filters=cmd_filter))
        # Plain text (anything not starting with `/`) is forwarded as a prompt.
        # Same allowlist filter so non-listed chats can't slip past via plain
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
