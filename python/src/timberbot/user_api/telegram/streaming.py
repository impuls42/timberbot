from __future__ import annotations

import asyncio
import contextlib
import logging

log = logging.getLogger("timberbot.user_api")


class StreamBuffer:
    """Throttled streaming-text renderer for one Telegram message.

    Accumulates incoming chunks and periodically edits the bound message so
    the user sees the agent's reply grow in place. Two real-world Telegram
    constraints shape the implementation:

    - Single text messages cap at 4096 characters. Once the accumulated text
      crosses that line we freeze the current message and roll over to a new
      one for the tail. We use a slightly lower cap (`MAX_CHARS`) to leave
      headroom for UTF-16 surrogate-pair accounting Telegram does server-side.
    - Editing a message with the exact same content raises
      `BadRequest: Message is not modified`. Once streaming stops, the
      periodic flush task would otherwise produce one error log per
      FLUSH_INTERVAL, so we skip the API call when `_text == _last_sent`.
    """

    CHAR_THRESHOLD = 500     # bytes since last flush that force an early flush
    FLUSH_INTERVAL = 0.5     # seconds between periodic flushes
    MAX_CHARS = 4000         # per-message char cap (under Telegram's 4096)
    SPLIT_LOOKBACK = 200     # how far back to hunt for a clean break point

    def __init__(self, chat_id: int, bot: object, message_id: int | None = None) -> None:
        self._chat_id = chat_id
        self._bot = bot
        # `_text` is the FULL accumulated text. `_cur_start` is the index
        # within `_text` where the *current* message_id begins; everything
        # before that has already been frozen in an earlier message.
        self._text: str = ""
        self._cur_message_id: int | None = message_id
        self._cur_start: int = 0
        self._last_sent: str = ""
        self._since_flush: int = 0
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    def feed(self, chunk: str) -> None:
        self._text += chunk
        self._since_flush += len(chunk)
        if self._since_flush >= self.CHAR_THRESHOLD:
            asyncio.create_task(self.flush())

    async def flush(self) -> None:
        if self._cur_message_id is None:
            return
        if not self._text or self._cur_start >= len(self._text):
            return

        # Compute the slice for the *current* message. If the accumulated
        # text past `_cur_start` exceeds the per-message cap, split at the
        # nearest word/line boundary so we don't chop a word in half.
        remaining = len(self._text) - self._cur_start
        if remaining <= self.MAX_CHARS:
            head_len = remaining
            overflowed = False
        else:
            head_len = self._safe_split_len(self._cur_start, self.MAX_CHARS)
            overflowed = True

        head = self._text[self._cur_start:self._cur_start + head_len]

        if head and head != self._last_sent:
            try:
                await self._bot.edit_message_text(  # type: ignore[union-attr]
                    chat_id=self._chat_id,
                    message_id=self._cur_message_id,
                    text=head,
                )
                self._last_sent = head
            except Exception:
                log.exception(
                    "Failed to edit message %s in chat %s",
                    self._cur_message_id, self._chat_id,
                )

        if overflowed:
            # Open a new message for the tail. We cap the initial body at
            # MAX_CHARS too; if there's still more text after that, the next
            # flush will overflow again into yet another message.
            new_start = self._cur_start + head_len
            tail_remaining = len(self._text) - new_start
            tail_len = min(tail_remaining, self.MAX_CHARS)
            tail = self._text[new_start:new_start + tail_len]
            if tail:
                try:
                    new_msg = await self._bot.send_message(  # type: ignore[union-attr]
                        chat_id=self._chat_id, text=tail,
                    )
                    self._cur_message_id = getattr(new_msg, "message_id", None)
                    self._cur_start = new_start
                    self._last_sent = tail
                except Exception:
                    log.exception(
                        "Failed to open overflow message in chat %s", self._chat_id,
                    )

        self._since_flush = 0

    def _safe_split_len(self, start: int, cap: int) -> int:
        """Pick a cut length in [cap - SPLIT_LOOKBACK, cap] that ends on
        whitespace if possible, otherwise return `cap` (hard cut)."""
        end = start + cap
        lo = max(start, end - self.SPLIT_LOOKBACK)
        # Prefer a paragraph break, then a line break, then a space.
        for breaker in ("\n\n", "\n", " "):
            idx = self._text.rfind(breaker, lo, end)
            if idx != -1 and idx > start:
                return (idx + len(breaker)) - start
        return cap

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self.flush()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._periodic())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()
