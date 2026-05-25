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
    # Optional originating user — when set, the adapter targets only that
    # user's chat instead of broadcasting to all bound chats. Carried for
    # multi-user-ready routing; today the `complain` MCP tool can't yet
    # discover which user it's serving (see design/subagent-delegation.md §7
    # for the same routing concern in delegation), so this is wired but
    # unused by the current `_on_complaint` callback.
    user_id: str | None = None


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
    # When the action originated from a subagent (Phase 2), the
    # `<slug>-<nonce>` id of that subagent. The adapter renders a
    # `[<subagent_id>] …` prefix so the user can tell the source apart from
    # the main agent's tool calls.
    subagent_id: str | None = None


@dataclass
class SubagentStatusChange:
    """One subagent status-machine transition surfaced to the user.

    `_user_message_loop` registers the registry's `on_status_change`
    observer to emit this for every flip (e.g. `running → completed`,
    `running → errored`). The Telegram adapter formats a single concise
    line per transition so the player can see fan-out progress at a glance.
    """

    user_id: str
    subagent_id: str
    agent: str  # the spec slug — "scout" / "wirer" / "auditor"
    prev_status: str
    new_status: str
    # Free-form context, e.g. last_error="timeout after 60s" for errored,
    # or None for clean transitions.
    detail: str | None = None


ConnectorMessage = (
    TextChunk | SessionStateChange | GameElicitation | AgentFeedback
    | ToolAction | SubagentStatusChange
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
