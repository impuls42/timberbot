from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass
class TextChunk:
    session_id: str
    text: str
    # Optional fallback when session_id can't resolve to a chat yet (e.g. a
    # message arrives before the first ACP session is registered). Adapters
    # may use it to route by the originating user instead.
    user_id: str | None = None


@dataclass
class SessionStateChange:
    session_id: str
    # active | halting | ended | no session | error | info
    state: str
    detail: str | None = None
    user_id: str | None = None


@dataclass
class GameElicitation:
    session_id: str
    question: str
    choices: list[str]
    correlation_id: str  # echo back with user's answer
    user_id: str | None = None


@dataclass
class AgentFeedback:
    category: str
    severity: str
    message: str


@dataclass
class ToolAction:
    """A completed (or failed) tool call the agent ran.

    Distinct from `TextChunk` because the user adapter should render it as a
    fresh standalone message — these are notifications of in-world actions,
    not part of the streaming reply text.
    """

    session_id: str
    # Already-formatted single line (verb + brief args), e.g.
    # "🔧 place_building(prefab=LogPile, x=50, y=40, z=4)".
    summary: str
    # True for status="completed", False for "failed".
    ok: bool = True
    user_id: str | None = None


ConnectorMessage = (
    TextChunk | SessionStateChange | GameElicitation | AgentFeedback | ToolAction
)


@dataclass
class UserMessage:
    user_id: str
    text: str
    chat_id: int | None = None  # opaque adapter-specific delivery handle
    session_id: str | None = None  # None = let session manager create one


class UserAdapter(Protocol):
    async def send(self, msg: ConnectorMessage) -> None: ...
    def messages(self) -> AsyncIterator[UserMessage]: ...
