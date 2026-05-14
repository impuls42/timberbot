"""Pytest wrappers for the TestRunner integration harness.

Each `TestRunner.test_*` method becomes an independent pytest test case via
`@pytest.mark.parametrize`, so the test report lists 60+ cases (one per
endpoint behavior) instead of a single monolithic run. Failures show the
underlying method name in the report line for easy triage.

Group markers (`@pytest.mark.read`, `@pytest.mark.write`, `@pytest.mark.perf`,
etc.) match `TestRunner.GROUPS` so you can run a subset:

    pytest python/tests/integration -m "integration and read"
    pytest python/tests/integration -m "integration and not perf"
"""
from __future__ import annotations

import pytest

from .validation_runner import TestRunner

pytestmark = [pytest.mark.integration, pytest.mark.live_game]


def _build_params() -> list[pytest.param]:  # type: ignore[name-defined]
    """One `pytest.param` per `TestRunner.test_*` method, statically tagged with the group marker.

    Static tagging means `pytest -m "integration and write"` selects at
    collection time (instead of requiring per-test `request.applymarker`,
    which runs too late).
    """
    method_to_group: dict[str, str] = {}
    for group, methods in TestRunner.GROUPS.items():
        for name in methods:
            method_to_group[f"test_{name}"] = group

    params: list = []
    for name in sorted(dir(TestRunner)):
        if not name.startswith("test_"):
            continue
        group = method_to_group.get(name, "ungrouped")
        params.append(
            pytest.param(
                name,
                id=name.removeprefix("test_"),
                marks=getattr(pytest.mark, group),
            )
        )
    return params


_PARAMS = _build_params()


@pytest.mark.parametrize("method_name", _PARAMS)
def test_validation_method(
    validation_runner: TestRunner,
    method_name: str,
) -> None:
    """Run a single `TestRunner.test_*` method as its own pytest test.

    `runner.failed` / `runner.passed` are snapshotted before the call; if the
    failure delta is non-zero we surface the count in the assertion message.
    Group markers are applied statically by `_build_params()` so collection-
    time filtering (`-m "integration and write"`) works.
    """
    method = getattr(validation_runner, method_name)
    failed_before = validation_runner.failed
    passed_before = validation_runner.passed
    method()
    failed_delta = validation_runner.failed - failed_before
    passed_delta = validation_runner.passed - passed_before

    assert failed_delta == 0, (
        f"TestRunner.{method_name} reported {failed_delta} failure(s); "
        f"{passed_delta} pass(es). Check the captured stdout for `FAIL:` lines."
    )
