from __future__ import annotations

__all__ = ["TelegramAdapter"]


def __getattr__(name: str) -> object:
    if name == "TelegramAdapter":
        from timberbot.user_api.telegram.bot import TelegramAdapter  # noqa: PLC0415
        return TelegramAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
