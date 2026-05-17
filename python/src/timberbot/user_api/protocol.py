from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass
class TextChunk:
    session_id: str
    text: str


@dataclass
class SessionStateChange:
    session_id: str
    # active | halting | ended | no session | error
    state: str
    detail: str | None = None


@dataclass
class GameElicitation:
    session_id: str
    question: str
    choices: list[str]
    correlation_id: str  # echo back with user's answer


ConnectorMessage = TextChunk | SessionStateChange | GameElicitation


@dataclass
class UserMessage:
    user_id: str
    text: str
    chat_id: int | None = None  # opaque adapter-specific delivery handle
    session_id: str | None = None  # None = let session manager create one


class UserAdapter(Protocol):
    async def send(self, msg: ConnectorMessage) -> None: ...
    def messages(self) -> AsyncIterator[UserMessage]: ...
