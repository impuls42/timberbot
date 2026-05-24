"""Tests for `tbot agent <sub>` via the Fire-exposed `AgentCommands` class."""
from __future__ import annotations

from unittest.mock import patch

from timberbot.cli.commands.agent import AgentCommands


def test_run_dispatches_to_runner_with_minimal_args():
    """`AgentCommands().run(...)` forwards args to `run_agent` with sensible defaults."""
    with patch("timberbot.cli.commands.agent.run_agent", return_value=0) as ra:
        rc = AgentCommands().run(goal="do a thing", backend="claude")
    assert rc == 0
    ra.assert_called_once()
    kwargs = ra.call_args.kwargs
    assert kwargs["goal"] == "do a thing"
    assert kwargs["backend"] == "claude"
    assert kwargs["model"] is None
    assert kwargs["effort"] is None
    assert kwargs["command_template"] is None
    assert kwargs["terminal_prefix"] is None
    assert kwargs["prompt_name"] == "timberbot"


def test_run_forwards_all_optional_args():
    with patch("timberbot.cli.commands.agent.run_agent", return_value=0) as ra:
        AgentCommands().run(
            goal="x",
            backend="custom",
            model="opus",
            effort="high",
            binary="/opt/aider/aider",
            command="aider {prompt}",
            terminal_prefix="wt -d {cwd} --",
            prompt="wirer",
        )
    kwargs = ra.call_args.kwargs
    assert kwargs["backend"] == "custom"
    assert kwargs["model"] == "opus"
    assert kwargs["effort"] == "high"
    assert kwargs["binary"] == "/opt/aider/aider"
    assert kwargs["command_template"] == "aider {prompt}"
    assert kwargs["terminal_prefix"] == "wt -d {cwd} --"
    assert kwargs["prompt_name"] == "wirer"


def test_run_catches_value_error_from_runner(capsys):
    """If `run_agent` raises ValueError (e.g. unknown backend), surface it cleanly."""
    with patch(
        "timberbot.cli.commands.agent.run_agent",
        side_effect=ValueError("unknown backend"),
    ):
        rc = AgentCommands().run(goal="x", backend="bogus")
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown backend" in err


def test_list_backends_subcommand(capsys):
    # Returns None — Fire prints whatever the method returns, so we keep it
    # implicit so the CLI doesn't echo a trailing "0" after the listing.
    assert AgentCommands().list_backends() is None
    out = capsys.readouterr().out
    for name in ("claude", "codex", "opencode", "custom"):
        assert name in out
