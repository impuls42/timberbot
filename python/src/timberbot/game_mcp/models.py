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


class EventEnvelope(BaseModel, Generic[T]):
    """Full tool response: game result + event meta."""
    result: T
    meta: EventMeta
