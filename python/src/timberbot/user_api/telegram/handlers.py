from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.ext import ContextTypes

from timberbot.user_api.protocol import UserMessage

log = logging.getLogger("timberbot.user_api")

# Emoji used to signal "the bot is working on this; reply is pending" — only
# fires when a message gets forwarded to the agent (where the user has to
# wait). Commands that reply synchronously (/status, /cancel, /state) don't
# need a reaction: the reply itself is the ack.
ACK_REACTION = "👀"


async def _ack(update: Update) -> None:
    """React to a user message that is about to be handled asynchronously.

    Only call this when there's actual work ahead of the reply (i.e. the
    message will be queued to the agent). Synchronous-reply handlers should
    not ack: the reply text lands immediately and a reaction would be
    redundant.

    Some chat configurations (older clients, channels) refuse bot reactions
    with BadRequest; treat that as soft-fail.
    """
    msg = update.message
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.set_reaction(reaction=ACK_REACTION)


def make_handlers(
    queue: asyncio.Queue,  # type: ignore[type-arg]
    dialog_id: str,
) -> dict:  # type: ignore[type-arg]
    """Build the inbound handler set.

    `dialog_id` is the configured chat id (stringified). The
    TelegramAdapter already gates command + text Updates by
    `filters.Chat(chat_id=…)`, so handlers don't need to re-check —
    except for callback queries, which Telegram doesn't filter the same
    way, so the callback handler verifies `update.effective_chat.id`
    against the configured chat explicitly.
    """
    expected_chat_id = int(dialog_id)

    async def _enqueue(update: Update, text: str) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        await queue.put(UserMessage(
            dialog_id=str(chat.id),
            text=text,
            chat_id=chat.id,
        ))

    async def prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        text = " ".join(context.args or [])  # type: ignore[arg-type]
        if not text:
            await update.message.reply_text(
                "Usage: /prompt <your message>\n"
                "Tip: you can also just type your message directly — any non-slash "
                "text is sent to the agent."
            )
            return
        await _ack(update)
        await _enqueue(update, text)

    async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        # No reaction: the loop replies synchronously with "halting" or
        # "no session" — the reply itself is the ack.
        await _enqueue(update, "/cancel")

    async def halt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        await _enqueue(update, "/halt")

    async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        await _enqueue(update, "/status")

    async def state_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        await _enqueue(update, "/state")

    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Plain (non-slash) text is forwarded to the agent as a prompt."""
        if update.effective_chat is None or update.message is None:
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        await _ack(update)
        await _enqueue(update, text)

    async def choice_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if query is None or query.data is None or chat is None:
            return
        # Callback queries bypass the adapter's `filters.Chat`; recheck
        # here so a callback from any other chat (e.g. an inline keyboard
        # forwarded elsewhere) doesn't smuggle commands through.
        if chat.id != expected_chat_id:
            log.info("Dropping callback from non-bound dialog %s", chat.id)
            await query.answer()
            return
        await query.answer()
        # callback_data format: "choice:<correlation_id>:<choice_text>"
        parts = query.data.split(":", 2)
        if len(parts) != 3:
            log.warning("Unexpected callback_data format: %s", query.data)
            return
        _, correlation_id, choice = parts
        await queue.put(UserMessage(
            dialog_id=str(chat.id),
            text=f"choice:{correlation_id}:{choice}",
            chat_id=chat.id,
        ))

    return {
        "prompt": prompt_handler,
        "cancel": cancel_handler,
        "halt": halt_handler,
        "status": status_handler,
        "state": state_handler,
        "text": text_handler,
        "choice_callback": choice_callback_handler,
    }
