"""Unit tests for game_mcp.bus — EventBus ring buffer and cursor logic."""
from __future__ import annotations

import time

import pytest

from timberbot.game_mcp.bus import EventBus, classify_severity
from timberbot.game_mcp.models import GameEvent, Severity


def _event(event_type: str = "season.change", day: int = 1, sev: Severity | None = None) -> GameEvent:
    s = sev if sev is not None else classify_severity(event_type)
    return GameEvent(seq=0, type=event_type, day=day, timestamp=int(time.time()), severity=s, payload={})


# ---------------------------------------------------------------------------
# classify_severity
# ---------------------------------------------------------------------------


def test_classify_known_types():
    assert classify_severity("drought.start") == Severity.warn
    assert classify_severity("badtide.start") == Severity.critical
    assert classify_severity("building.collapsed") == Severity.critical
    assert classify_severity("beaver.died") == Severity.notice
    assert classify_severity("drought.end") == Severity.info
    assert classify_severity("season.change") == Severity.info


def test_classify_unknown_defaults_to_info():
    assert classify_severity("some.unknown.event") == Severity.info
    assert classify_severity("") == Severity.info


# ---------------------------------------------------------------------------
# push / high_water
# ---------------------------------------------------------------------------


def test_empty_bus_high_water():
    bus = EventBus()
    assert bus.high_water == 0


def test_push_increments_seq():
    bus = EventBus()
    seq1 = bus.push(_event())
    seq2 = bus.push(_event())
    assert seq1 == 1
    assert seq2 == 2
    assert bus.high_water == 2


def test_push_assigns_seq_to_stored_event():
    bus = EventBus()
    bus.push(_event())
    events, _, _, _ = bus.consume(0)
    assert events[0].seq == 1


# ---------------------------------------------------------------------------
# consume — basic
# ---------------------------------------------------------------------------


def test_consume_empty_bus():
    bus = EventBus()
    events, hw, truncated, dropped = bus.consume(0)
    assert events == []
    assert hw == 0
    assert truncated is False
    assert dropped == 0


def test_consume_cursor_at_high_water_returns_empty():
    bus = EventBus()
    bus.push(_event())
    events, hw, _, _ = bus.consume(1)
    assert events == []
    assert hw == 1


def test_consume_all_from_zero():
    bus = EventBus()
    for _ in range(5):
        bus.push(_event())
    events, hw, truncated, dropped = bus.consume(0)
    assert len(events) == 5
    assert hw == 5
    assert truncated is False
    assert dropped == 0
    # Events are in seq order
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


def test_consume_partial_from_cursor():
    bus = EventBus()
    for _ in range(5):
        bus.push(_event())
    events, hw, _, _ = bus.consume(3)
    assert [e.seq for e in events] == [4, 5]
    assert hw == 5


# ---------------------------------------------------------------------------
# consume — truncation
# ---------------------------------------------------------------------------


def test_consume_truncated_at_limit():
    bus = EventBus(capacity=256)
    for _ in range(70):
        bus.push(_event())
    events, hw, truncated, dropped = bus.consume(0, limit=64)
    assert len(events) == 64
    assert truncated is True
    assert hw == 70
    assert dropped == 0


def test_consume_not_truncated_when_within_limit():
    bus = EventBus()
    for _ in range(10):
        bus.push(_event())
    events, _, truncated, _ = bus.consume(0, limit=64)
    assert len(events) == 10
    assert truncated is False


# ---------------------------------------------------------------------------
# consume — ring buffer overflow and dropped count
# ---------------------------------------------------------------------------


def test_ring_rotation_evicts_oldest():
    bus = EventBus(capacity=4)
    for _ in range(6):
        bus.push(_event())
    assert bus.high_water == 6
    events, hw, _, _ = bus.consume(0)
    # Only the last 4 survive (seq 3-6)
    assert len(events) == 4
    assert events[0].seq == 3
    assert events[-1].seq == 6


def test_dropped_count_after_overflow():
    bus = EventBus(capacity=4)
    for _ in range(6):
        bus.push(_event())
    _, _, _, dropped = bus.consume(0)
    # Events at seq 1 and 2 were evicted; cursor was 0 so they're "dropped"
    assert dropped == 2


def test_dropped_zero_when_cursor_past_evicted():
    bus = EventBus(capacity=4)
    for _ in range(6):
        bus.push(_event())
    # Consume up to seq 6 first, then push more
    _, _, _, dropped = bus.consume(2)
    # First retained seq is 3; cursor is 2 → gap = 3-2-1 = 0
    assert dropped == 0


# ---------------------------------------------------------------------------
# advisory
# ---------------------------------------------------------------------------


def test_advisory_normal_on_empty():
    bus = EventBus()
    assert bus.advisory([]) == "normal"


def test_advisory_normal_on_info_only():
    bus = EventBus()
    events = [_event("season.change", sev=Severity.info), _event("drought.end", sev=Severity.info)]
    assert bus.advisory(events) == "normal"


def test_advisory_attention_on_notice():
    bus = EventBus()
    events = [_event("beaver.died", sev=Severity.notice)]
    assert bus.advisory(events) == "attention"


def test_advisory_urgent_on_warn():
    bus = EventBus()
    events = [_event("drought.start", sev=Severity.warn)]
    assert bus.advisory(events) == "urgent"


def test_advisory_halt_on_critical():
    bus = EventBus()
    events = [_event("building.collapsed", sev=Severity.critical)]
    assert bus.advisory(events) == "halt"


def test_advisory_uses_highest_severity():
    bus = EventBus()
    events = [
        _event("season.change", sev=Severity.info),
        _event("low_food", sev=Severity.warn),
        _event("beaver.died", sev=Severity.notice),
    ]
    assert bus.advisory(events) == "urgent"


# ---------------------------------------------------------------------------
# hint
# ---------------------------------------------------------------------------


def test_hint_none_for_normal():
    bus = EventBus()
    assert bus.hint("normal") is None


def test_hint_non_none_for_elevated():
    bus = EventBus()
    assert bus.hint("halt") is not None
    assert bus.hint("urgent") is not None
    assert bus.hint("attention") is not None


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_clears_bus():
    bus = EventBus()
    for _ in range(5):
        bus.push(_event())
    bus.reset()
    assert bus.high_water == 0
    events, _, _, _ = bus.consume(0)
    assert events == []
