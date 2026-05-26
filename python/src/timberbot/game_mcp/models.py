"""Pydantic models for game event bus and tool envelopes.

Every MCP tool returns an EventEnvelope wrapping the tool-specific result
alongside a meta block that carries game events accumulated since the agent's
last tool call. The agent reads meta.events before deciding its next move and
uses meta.advisory to gauge urgency.
"""
from __future__ import annotations

from enum import IntEnum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Severity(IntEnum):
    """Event importance. Higher = more urgent."""
    trace = 0
    info = 1
    notice = 2
    warn = 3
    critical = 4


Advisory = Literal["normal", "attention", "urgent", "halt"]


class GameEvent(BaseModel):
    """A single game event as seen by the agent."""
    seq: int = Field(default=0, description="Monotonically increasing sequence number. Assigned by EventBus.push().")
    type: str = Field(..., description="Namespaced event type, e.g. 'drought.start'.")
    day: int
    timestamp: int = Field(..., description="Unix epoch seconds.")
    severity: Severity
    payload: dict[str, Any] = Field(default_factory=dict)


class Cursor(BaseModel):
    consumed: int = Field(..., description="Last event seq the agent has seen.")
    high_water: int = Field(..., description="Current highest seq in the bus at response time.")


class SubagentEvent(BaseModel):
    """One asynchronous subagent activity update.

    The delegation runtime pushes one of these onto the calling dialog's
    queue whenever a subagent turn ends (clean, errored, or cancelled).
    Drained on every `mcp__game__*` tool response, so the main agent can
    pick up subagent activity in the same `meta` block it already scans
    for game events instead of having to poll `subagent_status` / `wait`
    explicitly.
    """
    subagent_id: str
    agent: str = Field(..., description="Spec slug — scout / wirer / auditor.")
    kind: Literal[
        "turn_completed", "turn_errored", "turn_cancelled", "closed",
    ]
    status: str = Field(..., description="Run status at the moment of the event.")
    stop_reason: str | None = None
    # Reply text trimmed to a few hundred chars so the envelope stays small.
    # Use `subagent_transcript(subagent_id)` to fetch the untrimmed reply.
    reply_excerpt: str | None = None
    last_error: str | None = None
    timestamp: float = Field(..., description="Monotonic time the event was recorded.")


class EventMeta(BaseModel):
    """Metadata block attached to every tool response."""
    cursor: Cursor
    events: list[GameEvent]
    events_truncated: bool = Field(
        ..., description="True when more events exist beyond the returned slice."
    )
    events_dropped: int = Field(
        ..., description="Events evicted from the ring buffer before the cursor."
    )
    advisory: Advisory
    hint: str | None = None
    subagent_events: list[SubagentEvent] = Field(
        default_factory=list,
        description=(
            "Subagent turn-end events accumulated since this dialog's last "
            "MCP call. Empty when no subagents are in flight."
        ),
    )


class EventEnvelope(BaseModel, Generic[T]):
    """Full tool response: game result + event meta."""
    result: T
    meta: EventMeta
