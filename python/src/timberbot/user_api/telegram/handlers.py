from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from timberbot.user_api.protocol import UserMessage

log = logging.getLogger("timberbot.user_api")


def make_handlers(
    queue: asyncio.Queue,  # type: ignore[type-arg]
    allowed_users: set[int] | None = None,
) -> dict:  # type: ignore[type-arg]
    allowed = allowed_users or set()

    def _user_allowed(uid: int | None) -> bool:
        if not allowed:
            return True
        return uid is not None and uid in allowed

    async def prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None or update.effective_chat is None:
            return
        text = " ".join(context.args or [])  # type: ignore[arg-type]
        if not text:
            await update.message.reply_text("Usage: /prompt <your message>")
            return
        await queue.put(UserMessage(
            user_id=str(update.effective_user.id),
            text=text,
            chat_id=update.effective_chat.id,
        ))

    async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None or update.effective_chat is None:
            return
        await queue.put(UserMessage(
            user_id=str(update.effective_user.id),
            text="/cancel",
            chat_id=update.effective_chat.id,
        ))

    async def halt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None or update.effective_chat is None:
            return
        await queue.put(UserMessage(
            user_id=str(update.effective_user.id),
            text="/halt",
            chat_id=update.effective_chat.id,
        ))

    async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None or update.effective_chat is None:
            return
        await queue.put(UserMessage(
            user_id=str(update.effective_user.id),
            text="/status",
            chat_id=update.effective_chat.id,
        ))

    async def choice_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None or query.data is None or update.effective_chat is None:
            return
        if not _user_allowed(query.from_user.id):
            log.info("Dropping callback from non-allowed user %s", query.from_user.id)
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
            user_id=str(query.from_user.id),
            text=f"choice:{correlation_id}:{choice}",
            chat_id=update.effective_chat.id,
        ))

    return {
        "prompt": prompt_handler,
        "cancel": cancel_handler,
        "halt": halt_handler,
        "status": status_handler,
        "choice_callback": choice_callback_handler,
    }
