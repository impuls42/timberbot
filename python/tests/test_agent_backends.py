"""Unit tests for agent backend argv builders."""
from __future__ import annotations

from pathlib import Path

import pytest

from tbot.agent.backend import AgentContext, known_backend_names
from tbot.agent.backends import claude, codex, custom, opencode  # noqa: F401  (registers)
from tbot.agent.runner import resolve_backend


def _ctx(tmp_path: Path, **overrides) -> AgentContext:
    base = {
        "goal": "reach 50 beavers",
        "instructions_file": tmp_path / "agent-instructions.md",
        "cwd": tmp_path,
    }
    base.update(overrides)
    return AgentContext(**base)


def test_all_four_backends_register():
    assert set(known_backend_names()) == {"claude", "codex", "opencode", "custom"}


def test_claude_argv_minimal(tmp_path):
    b = resolve_backend("claude")
    argv = b.build_argv(_ctx(tmp_path))
    assert argv[0] == "claude"
    assert "--system-prompt-file" in argv
    assert argv[-1] == "reach 50 beavers"
    assert "--model" not in argv
    assert "--effort" not in argv


def test_claude_argv_with_model_and_effort(tmp_path):
    b = resolve_backend("claude")
    argv = b.build_argv(_ctx(tmp_path, model="claude-haiku-4-5", effort="high"))
    assert "--model" in argv and "claude-haiku-4-5" in argv
    assert "--effort" in argv and "high" in argv


def test_codex_argv_uses_dash_c(tmp_path):
    b = resolve_backend("codex")
    argv = b.build_argv(_ctx(tmp_path, model="gpt-5", effort="medium"))
    # Codex syntax: `-c model_instructions_file="..."` and `-c model_reasoning_effort="..."`
    joined = " ".join(argv)
    assert "model_instructions_file=" in joined
    assert "model_reasoning_effort=" in joined
    assert "--model" in argv and "gpt-5" in argv


def test_opencode_argv_starts_with_run(tmp_path):
    b = resolve_backend("opencode")
    argv = b.build_argv(_ctx(tmp_path))
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert "--prompt-file" in argv
    assert argv[-1] == "reach 50 beavers"


def test_custom_template_substitutes_placeholders(tmp_path):
    # Note: {prompt} must be quoted in the template if the goal contains spaces.
    # This is the legacy behavior — the C# `BuildCustomCommand` had the same rule.
    template = (
        'aider --system-prompt-file {instructions_file} --model {model} "{prompt}"'
    )
    b = resolve_backend("custom", command_template=template)
    argv = b.build_argv(_ctx(tmp_path, model="opus", effort="high"))
    assert argv[0] == "aider"
    assert "--system-prompt-file" in argv
    assert "--model" in argv and "opus" in argv
    assert argv[-1] == "reach 50 beavers"


def test_custom_template_handles_quoted_strings(tmp_path):
    # shlex.split should respect single quotes
    template = 'mybackend --prompt "{prompt}"'
    b = resolve_backend("custom", command_template=template)
    argv = b.build_argv(_ctx(tmp_path))
    assert argv == ["mybackend", "--prompt", "reach 50 beavers"]


def test_custom_requires_template():
    with pytest.raises(ValueError, match="custom"):
        resolve_backend("custom")


def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown backend"):
        resolve_backend("does-not-exist")


def test_binary_override_for_claude(tmp_path):
    b = resolve_backend("claude", binary_override="/opt/claude/claude")
    argv = b.build_argv(_ctx(tmp_path))
    assert argv[0] == "/opt/claude/claude"
