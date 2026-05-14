"""Pytest fixtures + auto-skip for the integration test suite.

The tests under this directory drive the live `/api/*` surface of a running
Timberborn game. They require:

  - Timberborn launched with the Timberbot mod loaded
  - A save game open (any settlement, any faction)
  - The HTTP API reachable at `localhost:8085` (or via `--tbot-host` / `--tbot-port`)

When the game is unreachable, every integration test is **skipped** rather than
failed. CI doesn't run these by default; see `pyproject.toml` for the marker
registration and the README in this directory for run instructions.
"""
from __future__ import annotations

from typing import Any

import pytest

from tbot import Timberbot


def pytest_addoption(parser: pytest.Parser) -> None:
    """Allow overriding the host/port from the pytest CLI.

    Examples:
        pytest python/tests/integration -m integration
        pytest python/tests/integration --tbot-host 192.168.1.10 --tbot-port 9090
    """
    group = parser.getgroup("timberbot")
    group.addoption(
        "--tbot-host",
        default=None,
        help="Host where the Timberbot mod is listening (overrides settings.json).",
    )
    group.addoption(
        "--tbot-port",
        type=int,
        default=None,
        help="Port where the Timberbot mod is listening (overrides settings.json).",
    )


@pytest.fixture(scope="session")
def tbot_endpoint(request: pytest.FixtureRequest) -> tuple[str | None, int | None]:
    """The (host, port) selected for this session, or (None, None) for defaults."""
    return request.config.getoption("--tbot-host"), request.config.getoption("--tbot-port")


@pytest.fixture(scope="session")
def live_game(tbot_endpoint: tuple[str | None, int | None]) -> Timberbot:
    """A `Timberbot` client bound to a live game, or `pytest.skip` if unreachable.

    All integration tests should request this fixture (directly or indirectly).
    The first request per session pings the API; failure skips the whole
    session's integration tests rather than failing them, so CI without a game
    is green.
    """
    host, port = tbot_endpoint
    bot = Timberbot(host=host, port=port, json_mode=True, write_timeout=300)
    if not bot.ping():
        target = f"{host or '127.0.0.1'}:{port or 8085}"
        pytest.skip(
            f"Timberbot mod not reachable at {target}. "
            "Launch Timberborn with the mod loaded and rerun. "
            "See python/tests/integration/README.md for full setup."
        )
    return bot


@pytest.fixture(scope="session")
def strict_bot(tbot_endpoint: tuple[str | None, int | None]) -> Timberbot:
    """A second `Timberbot` client used by validation_runner to compare TOON vs JSON."""
    host, port = tbot_endpoint
    return Timberbot(host=host, port=port, json_mode=True, write_timeout=300)


@pytest.fixture(scope="session")
def toon_bot(tbot_endpoint: tuple[str | None, int | None]) -> Timberbot:
    """A `Timberbot` client in toon mode for comparison-based tests."""
    host, port = tbot_endpoint
    return Timberbot(host=host, port=port, write_timeout=300)


@pytest.fixture(scope="session")
def v2_runner(live_game: Timberbot) -> Any:
    """The pre-PR-3 V2Runner harness, wired against the live game.

    Hand-rolls a single instance per pytest session so discover() runs once.
    """
    import sys
    from datetime import datetime

    from .v2_runner import V2Runner

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return V2Runner(
        run_id=timestamp,
        host=live_game.host,
        port=live_game.port,
        log_writer=sys.stdout,
        error_writer=sys.stderr,
    )


@pytest.fixture(scope="session")
def validation_runner(live_game: Timberbot, strict_bot: Timberbot, toon_bot: Timberbot) -> Any:
    """The pre-PR-3 TestRunner harness, wired against the live game.

    discover() is called eagerly so per-method tests share the same probe state.
    """
    from .validation_runner import TestRunner

    runner = TestRunner()
    # Override the default Timberbot constructors with our endpoint-resolved instances.
    runner.bot = live_game
    runner.bot._check = lambda data: data  # tolerate error payloads in checks
    runner.strict_bot = strict_bot
    runner.toon_bot = toon_bot
    # Discover sample IDs once for the whole session.
    runner.discover()
    return runner
