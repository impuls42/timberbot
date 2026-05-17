"""In-process ring-buffer event bus for the game MCP server.

The bus ingests EventPush frames from the game's WebSocket channel and makes
them available to MCP tools via cursor-based consumption. Each tool call
advances the agent's cursor, delivering only new events in the response envelope.

Thread-safety: this module is designed for a single asyncio event loop.
EventIngestor calls push() from the event loop; MCP tool functions must also
run on the same loop (use async tools, not sync+executor) to avoid races on
_seq and _buf.
"""
from __future__ import annotations

from collections import deque

from timberbot.game_mcp.models import Advisory, GameEvent, Severity

# ---------------------------------------------------------------------------
# Severity classification for known Timberborn event types
# ---------------------------------------------------------------------------

_SEVERITY_MAP: dict[str, Severity] = {
    # Droughts
    "drought.start": Severity.warn,
    "drought.end": Severity.info,
    # Badtides
    "badtide.start": Severity.critical,
    "badtide.end": Severity.info,
    # Floods
    "flood.start": Severity.warn,
    "flood.end": Severity.info,
    # Actors
    "beaver.died": Severity.notice,
    "beaver.spawned": Severity.info,
    # Buildings
    "building.collapsed": Severity.critical,
    "building.placed": Severity.info,
    "building.demolished": Severity.info,
    # Resources
    "low_food": Severity.warn,
    "no_food": Severity.critical,
    "low_water": Severity.warn,
    "no_water": Severity.critical,
    # Seasons/weather
    "season.change": Severity.info,
    "temperate.start": Severity.info,
    "temperate.end": Severity.info,
    # Fires
    "fire.start": Severity.critical,
    "fire.end": Severity.info,
}

# Human-readable hints per advisory level
_HINTS: dict[Advisory, str] = {
    "halt": "Critical event demands immediate response. Stop current plan and address.",
    "urgent": "High-severity events detected. Stop and re-evaluate before next action.",
    "attention": "Notable events since last action. Review before deciding next move.",
}


def classify_severity(event_type: str) -> Severity:
    """Return severity for a game event type string. Unknown types → info."""
    return _SEVERITY_MAP.get(event_type, Severity.info)


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Ring-buffer event bus. One instance per serve session.

    push() is called by EventIngestor; consume() is called by MCP tool wrappers.
    Both must run on the same asyncio event loop — no locking is done.
    """

    def __init__(self, capacity: int = 256) -> None:
        self._buf: deque[tuple[int, GameEvent]] = deque(maxlen=capacity)
        self._seq: int = 0

    @property
    def high_water(self) -> int:
        """The sequence number of the most recently pushed event."""
        return self._seq

    def push(self, event: GameEvent) -> int:
        """Append an event; returns the assigned seq. Evicts oldest on overflow."""
        self._seq += 1
        stored = event.model_copy(update={"seq": self._seq})
        self._buf.append((self._seq, stored))
        return self._seq

    def consume(
        self,
        cursor: int,
        limit: int = 64,
    ) -> tuple[list[GameEvent], int, bool, int]:
        """Return events after *cursor*.

        Returns:
            (events, high_water, truncated, dropped)
            - events: up to *limit* GameEvents with seq > cursor
            - high_water: current _seq at call time (new cursor value)
            - truncated: True when more events exist beyond the returned slice
            - dropped: events that fell off the ring buffer before cursor
        """
        hw = self._seq
        if cursor >= hw:
            return [], hw, False, 0

        after = [(seq, ev) for seq, ev in self._buf if seq > cursor]

        # Events between cursor and first retained seq were evicted (dropped)
        dropped = 0
        if after and after[0][0] > cursor + 1:
            dropped = after[0][0] - cursor - 1

        truncated = len(after) > limit
        visible = after[:limit]
        return [ev for _, ev in visible], hw, truncated, dropped

    def advisory(self, events: list[GameEvent]) -> Advisory:
        """Compute advisory level from a list of events."""
        if not events:
            return "normal"
        max_sev = max(ev.severity for ev in events)
        if max_sev >= Severity.critical:
            return "halt"
        if max_sev >= Severity.warn:
            return "urgent"
        if max_sev >= Severity.notice:
            return "attention"
        return "normal"

    def hint(self, advisory: Advisory) -> str | None:
        """Return a human-readable nudge for non-normal advisories."""
        return _HINTS.get(advisory)

    def reset(self) -> None:
        """Clear all events. Call on session teardown."""
        self._buf.clear()
        self._seq = 0
