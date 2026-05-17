from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def elicitation_keyboard(choices: list[str], correlation_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=choice, callback_data=f"choice:{correlation_id}:{choice}")]
        for choice in choices
    ]
    return InlineKeyboardMarkup(buttons)
