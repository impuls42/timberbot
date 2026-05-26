from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

# `dialog_id` is the deterministic identifier for a chat session — for
# Telegram it's `str(chat.id)`. Replaces the older `user_id` (Telegram user
# id) routing key: the bot always knows which chat to reply to even before
# any ACP session is registered, because the dialog_id IS the chat handle.


@dataclass
class TextChunk:
    session_id: str
    text: str
    # Originating dialog (chat) for this chunk. Adapters resolve the target
    # chat directly from this when the session_id binding isn't ready yet
    # (e.g. first agent reply racing `register_chat`).
    dialog_id: str | None = None
    # When set, the chunk came from a subagent session. Adapters render it
    # in a separate buffer so the user sees the subagent's text streaming
    # under a `[<subagent_id>]` header rather than mixed into the main
    # agent's reply.
    subagent_id: str | None = None


@dataclass
class SessionStateChange:
    session_id: str
    # active | halting | ended | no session | error | info
    state: str
    detail: str | None = None
    dialog_id: str | None = None


@dataclass
class GameElicitation:
    session_id: str
    question: str
    choices: list[str]
    correlation_id: str  # echo back with user's answer
    dialog_id: str | None = None


@dataclass
class AgentFeedback:
    """Agent-emitted bug/missing-feature notification.

    In single-dialog mode the bot is bound to one chat at startup, so
    feedback always routes there — no dialog_id field needed.
    """
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
    dialog_id: str | None = None
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

    dialog_id: str
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
    """An inbound message from the player.

    `dialog_id` is the canonical routing key — for Telegram it's the
    `str(chat_id)`. `chat_id` is kept as a typed convenience so adapters
    that need the raw int (e.g. `bot.send_message(chat_id=...)`) don't
    have to parse the string back; downstream code should treat
    `dialog_id` as the source of truth.
    """

    dialog_id: str
    text: str
    chat_id: int | None = None
    session_id: str | None = None  # None = let session manager create one


class UserAdapter(Protocol):
    async def send(self, msg: ConnectorMessage) -> None: ...
    def messages(self) -> AsyncIterator[UserMessage]: ...
