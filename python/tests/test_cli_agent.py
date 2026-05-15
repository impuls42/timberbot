"""Tests for `tbot agent run` argument parsing."""
from __future__ import annotations

import pytest

from timberbot.cli.commands import agent as agent_cmd


def test_run_requires_backend(capsys):
    # argparse should error out (exit 2) when --backend is missing.
    with pytest.raises(SystemExit) as exc:
        agent_cmd._parse_run(["--goal", "do a thing"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--backend" in err


def test_run_requires_goal(capsys):
    with pytest.raises(SystemExit) as exc:
        agent_cmd._parse_run(["--backend", "claude"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--goal" in err


def test_run_accepts_minimal_args():
    ns = agent_cmd._parse_run(["--goal", "do a thing", "--backend", "claude"])
    assert ns.goal == "do a thing"
    assert ns.backend == "claude"
    assert ns.model is None
    assert ns.effort is None
    assert ns.command_template is None
    assert ns.terminal_prefix is None
    assert ns.prompt_name == "timberbot"


def test_run_parses_all_optionals():
    ns = agent_cmd._parse_run([
        "--goal", "x",
        "--backend", "custom",
        "--model", "opus",
        "--effort", "high",
        "--binary", "/opt/aider/aider",
        "--command", "aider {prompt}",
        "--terminal-prefix", "wt -d {cwd} --",
        "--prompt", "wirer",
    ])
    assert ns.backend == "custom"
    assert ns.model == "opus"
    assert ns.effort == "high"
    assert ns.binary == "/opt/aider/aider"
    assert ns.command_template == "aider {prompt}"
    assert ns.terminal_prefix == "wt -d {cwd} --"
    assert ns.prompt_name == "wirer"


def test_run_dispatcher_rejects_unknown_subcommand(capsys):
    rc = agent_cmd.run(["does-not-exist"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown subcommand" in err


def test_run_dispatcher_lists_usage_when_empty(capsys):
    rc = agent_cmd.run([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "tbot agent" in err
    assert "run" in err
    assert "list-backends" in err
    assert "prompts" in err


def test_list_backends_subcommand(capsys):
    rc = agent_cmd.run(["list-backends"])
    assert rc == 0
    out = capsys.readouterr().out
    # All four backends should be listed.
    for name in ("claude", "codex", "opencode", "custom"):
        assert name in out
