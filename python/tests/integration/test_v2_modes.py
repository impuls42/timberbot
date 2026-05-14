"""Pytest wrappers for the V2 integration harness.

Each test drives one mode of the legacy `V2Runner`. They are slow (each one
hits dozens of endpoints), so they're collected only when the `integration`
marker is selected:

    pytest python/tests/integration -m integration
    pytest python/tests/integration -m "integration and not slow"

The runner records failures via `runner.failed`/`runner.passed`; we assert
on those counts after each mode.
"""
from __future__ import annotations

import pytest

from .v2_runner import ENDPOINT_SPECS, V2Runner

pytestmark = [pytest.mark.integration, pytest.mark.live_game]


def _assert_runner_passed(runner: V2Runner, mode: str) -> None:
    assert runner.failed == 0, (
        f"V2Runner.{mode} reported {runner.failed} failure(s) and "
        f"{runner.passed} pass(es). See `runner.sections` or the test-results "
        "log for details."
    )


def test_v2_smoke(v2_runner: V2Runner) -> None:
    """Hit every GET endpoint, verify shapes + status codes."""
    v2_runner.failed = 0
    v2_runner.passed = 0
    v2_runner.run_smoke(ENDPOINT_SPECS)
    _assert_runner_passed(v2_runner, "smoke")


def test_v2_freshness(v2_runner: V2Runner) -> None:
    """Mutate a tracked target, verify the next GET reflects it."""
    v2_runner.failed = 0
    v2_runner.passed = 0
    v2_runner.run_freshness()
    _assert_runner_passed(v2_runner, "freshness")


def test_v2_write_to_read(v2_runner: V2Runner) -> None:
    """POST a change, GET to confirm, restore, GET to confirm restore."""
    v2_runner.failed = 0
    v2_runner.passed = 0
    v2_runner.run_write_to_read()
    _assert_runner_passed(v2_runner, "write_to_read")


@pytest.mark.slow
def test_v2_performance(v2_runner: V2Runner) -> None:
    """Latency benchmark across all endpoints (default: 100 iterations)."""
    v2_runner.failed = 0
    v2_runner.passed = 0
    v2_runner.run_performance(ENDPOINT_SPECS, iterations=100)
    _assert_runner_passed(v2_runner, "performance")


@pytest.mark.slow
def test_v2_concurrency(v2_runner: V2Runner) -> None:
    """Concurrent GET fan-out, verify no crashes."""
    v2_runner.failed = 0
    v2_runner.passed = 0
    v2_runner.run_concurrency(ENDPOINT_SPECS, workers=8, iterations=25)
    _assert_runner_passed(v2_runner, "concurrency")
