from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from telegram.error import BadRequest, TelegramError
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


class DialogUnreachableError(RuntimeError):
    """Raised by `TelegramAdapter.probe()` when the configured chat id
    isn't reachable at startup.

    Surfaced when `bot.get_chat(int(dialog_id))` returns BadRequest
    (typo'd id, bot kicked from the chat, or the user has never DM'd
    the bot so Telegram won't surface the chat to bot api yet). The
    CLI converts this to a friendly one-line error like
    `ModUnreachableError`.
    """


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
    """Bound to a single configured Telegram chat for the lifetime of `tbot serve`.

    The chat (`dialog_id`, the stringified `chat.id`) is the deterministic
    delivery handle — the bot knows from startup where to send replies,
    so async paths (subagent completions, status changes, future game
    alerts) can push preemptively without waiting for an inbound ping.

    Telegram's `filters.Chat(chat_id=…)` enforces single-chat binding for
    command + text Updates. The callback-query handler does its own check
    because Telegram doesn't apply the filter to callback Updates the
    same way.
    """

    def __init__(self, token: str, dialog_id: str) -> None:
        # `dialog_id` is a string per the protocol (TOML can encode
        # supergroup ids like -1001234567890 only as strings; JSON
        # tends to mangle the int). We hold both forms: the int for
        # Telegram API calls, the string for outbound protocol
        # messages.
        if not dialog_id:
            raise ValueError("TelegramAdapter requires a non-empty dialog_id")
        try:
            self._chat_id: int = int(dialog_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"dialog_id must be a numeric Telegram chat id; got {dialog_id!r}"
            ) from exc
        self._dialog_id: str = dialog_id

        self._app = Application.builder().token(token).build()
        self._queue: asyncio.Queue[UserMessage] = asyncio.Queue()
        # Streaming-text buffers, keyed by `_stream_key`. Main-agent
        # session gets one; each subagent gets its own so subagent
        # placeholders don't collide with the main reply stream.
        self._buffers: dict[str, StreamBuffer] = {}

    @property
    def dialog_id(self) -> str:
        return self._dialog_id

    @property
    def chat_id(self) -> int:
        return self._chat_id

    async def probe(self) -> None:
        """Verify the bot can see the configured chat before declaring ready.

        Catches the common typo / wrong-chat-id case at startup with a
        clear error, instead of letting the first send_message fail
        opaquely. Mirrors `_probe_mod_until_reachable` in `serve.py`.
        """
        try:
            await self._app.bot.get_chat(self._chat_id)
        except BadRequest as exc:
            raise DialogUnreachableError(
                f"Telegram chat {self._chat_id} is not reachable by this "
                f"bot ({exc}). Check that the `dialog_id` in config.toml "
                "is correct and that the chat has messaged the bot at "
                "least once (Telegram requires prior contact before a "
                "bot can DM a user)."
            ) from exc
        except TelegramError as exc:
            raise DialogUnreachableError(
                f"Telegram error while probing chat {self._chat_id}: {exc}"
            ) from exc

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

    async def _handle_text_chunk(self, msg: TextChunk) -> None:
        key = _stream_key(msg.session_id, msg.subagent_id)
        buf = self._buffers.get(key)
        if buf is None:
            # Subagent chunks open their own buffer under a `[<id>] …`
            # placeholder so the user sees one stream per subagent
            # rather than mixed text in the main reply.
            placeholder_text = f"[{msg.subagent_id}] …" if msg.subagent_id else "…"
            placeholder = await self._app.bot.send_message(
                chat_id=self._chat_id, text=placeholder_text,
            )
            buf = StreamBuffer(
                chat_id=self._chat_id, bot=self._app.bot,
                message_id=placeholder.message_id,
            )
            self._buffers[key] = buf
            await buf.start()
            # For subagents, prime the buffer with the prefix so
            # subsequent chunks land under the same header.
            if msg.subagent_id:
                buf.feed(f"[{msg.subagent_id}] ")
        buf.feed(msg.text)

    async def _handle_elicitation(self, msg: GameElicitation) -> None:
        keyboard = elicitation_keyboard(msg.choices, msg.correlation_id)
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=msg.question,
            reply_markup=keyboard,
        )

    async def _handle_state_change(self, msg: SessionStateChange) -> None:
        # "info" carries a free-form body (e.g. /state output); render
        # the detail directly. Everything else gets the
        # "Session X: detail" shell.
        if msg.state == "info" and msg.detail:
            text = msg.detail
        else:
            detail = f": {msg.detail}" if msg.detail else ""
            text = f"Session {msg.state}{detail}"
        await self._app.bot.send_message(chat_id=self._chat_id, text=text)

        if msg.state == "ended":
            # Close every stream buffer (main + subagent) that belongs
            # to this session_id.
            stale = [
                k for k in self._buffers
                if k == msg.session_id or k.startswith(f"{msg.session_id}#")
            ]
            for key in stale:
                buf = self._buffers.pop(key, None)
                if buf is not None:
                    await buf.stop()

    async def _handle_feedback(self, msg: AgentFeedback) -> None:
        text = f"[feedback/{msg.category}/{msg.severity}] {msg.message}"
        try:
            await self._app.bot.send_message(chat_id=self._chat_id, text=text)
        except Exception:
            log.warning("Failed to deliver feedback notification to chat_id %s", self._chat_id)

    async def _handle_tool_action(self, msg: ToolAction) -> None:
        """Render a tool-action notification as a fresh chat message.

        Distinct from `_handle_text_chunk` because these aren't part of
        the agent's streaming reply — they're standalone "the bot did
        X" events that should land as their own message between turns,
        not be edited into the running stream buffer. Subagent actions
        get a `[<subagent_id>] …` prefix so the user can tell which
        agent ran.
        """
        text = (
            f"[{msg.subagent_id}] {msg.summary}" if msg.subagent_id else msg.summary
        )
        await self._app.bot.send_message(chat_id=self._chat_id, text=text)

    async def _handle_subagent_status(self, msg: SubagentStatusChange) -> None:
        """One concise line per subagent status transition.

        Skips the noisy `idle → running` / `running → idle` flips and
        surfaces the ones that signal real progress (`completed`,
        `errored`, `cancelled`, `closed`). The user gets a fan-out
        view without seeing every internal scheduler tick.
        """
        if msg.new_status not in ("completed", "errored", "cancelled", "closed"):
            return
        verb = msg.new_status
        text = f"[{msg.subagent_id}] {verb}"
        if msg.detail:
            text = f"{text}: {msg.detail}"
        try:
            await self._app.bot.send_message(chat_id=self._chat_id, text=text)
        except Exception:
            log.warning("Failed to deliver subagent status to chat_id %s", self._chat_id)

    def reset_stream(self, session_id: str) -> None:
        """Drop the streaming buffers for a session so the next chunk
        starts fresh.

        Called between user turns: continuing to edit the placeholder
        from the previous turn (which now sits above the user's newest
        message in the chat) is confusing. Drops the main-session
        buffer AND every subagent buffer that opened under it.
        """
        stale = [
            k for k in self._buffers
            if k == session_id or k.startswith(f"{session_id}#")
        ]
        for key in stale:
            buf = self._buffers.pop(key, None)
            if buf is not None:
                asyncio.create_task(buf.stop())

    async def messages(self) -> AsyncIterator[UserMessage]:  # type: ignore[override]
        while True:
            m = await self._queue.get()
            yield m

    async def start(self) -> None:
        handlers = make_handlers(self._queue, self._dialog_id)
        # Single-chat binding — only Updates from the configured chat
        # are processed. Group chats are out of scope; a different
        # chat id silently doesn't match and the Update is ignored.
        cmd_filter = filters.Chat(chat_id=self._chat_id)
        for name in ("prompt", "cancel", "halt", "status", "state"):
            self._app.add_handler(CommandHandler(name, handlers[name], filters=cmd_filter))
        # Plain text (anything not starting with `/`) is forwarded as a
        # prompt. Same chat filter so other chats can't slip past via
        # plain text. Registered after command handlers so /prompt
        # /state etc. still take priority for slash messages.
        text_filter = filters.TEXT & ~filters.COMMAND & cmd_filter
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
