"""Unit tests for the opencode `--attach <url>` plumbing.

Covers the four-way precedence matrix (CLI vs config.toml vs both vs neither)
plus the empty-string-as-unset behavior, all the way from
`_resolve_backend_defaults` into the final argv built by `OpencodeBackend`.

These tests stay at the argv layer — no subprocess is spawned.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from timberbot.agent import runner
from timberbot.agent.backend import AgentContext
from timberbot.agent.backends import opencode  # noqa: F401  (registers)
from timberbot.agent.runner import resolve_backend


def _ctx(tmp_path: Path, **overrides) -> AgentContext:
    base: dict[str, object] = {
        "goal": "reach 50 beavers",
        "instructions_file": tmp_path / "agent-instructions.md",
        "cwd": tmp_path,
    }
    base.update(overrides)
    return AgentContext(**base)  # type: ignore[arg-type]


def _write_config(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))


# --- argv-builder tests --------------------------------------------------


def test_opencode_argv_without_attach_url(tmp_path):
    """No attach_url → argv has no --attach token; bare `run <goal>` shape."""
    b = resolve_backend("opencode")
    argv = b.build_argv(_ctx(tmp_path))
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert "--attach" not in argv
    assert argv[-1] == "reach 50 beavers"


def test_opencode_argv_with_attach_url(tmp_path):
    """attach_url set on the context → `--attach <url>` appears right after `run`."""
    b = resolve_backend("opencode")
    argv = b.build_argv(_ctx(tmp_path, attach_url="http://127.0.0.1:4096"))
    assert argv[:4] == ["opencode", "run", "--attach", "http://127.0.0.1:4096"]
    assert argv[-1] == "reach 50 beavers"


def test_opencode_argv_attach_url_combines_with_model(tmp_path):
    """--attach and --model coexist; order is --attach then --model."""
    b = resolve_backend("opencode")
    argv = b.build_argv(_ctx(tmp_path, attach_url="http://h:4096", model="glm-4.6"))
    assert "--attach" in argv and "http://h:4096" in argv
    assert "--model" in argv and "glm-4.6" in argv
    # --attach precedes --model so the server URL is resolved first.
    assert argv.index("--attach") < argv.index("--model")


def test_opencode_argv_empty_attach_url_is_treated_as_unset(tmp_path):
    """Empty string on the AgentContext → no --attach in the argv."""
    b = resolve_backend("opencode")
    argv = b.build_argv(_ctx(tmp_path, attach_url=""))
    assert "--attach" not in argv


def test_opencode_argv_bakes_instructions_into_message(tmp_path):
    """When the instructions file exists, its contents are spliced into the
    positional message (opencode has no --prompt-file flag)."""
    instr = tmp_path / "agent-instructions.md"
    instr.write_text("# SYSTEM\n\nbe a good beaver", encoding="utf-8")
    b = resolve_backend("opencode")
    argv = b.build_argv(_ctx(tmp_path))
    # Last positional should carry both the instructions and the goal.
    assert "be a good beaver" in argv[-1]
    assert "reach 50 beavers" in argv[-1]


# --- precedence tests via _resolve_backend_defaults ----------------------


def test_resolve_only_config_toml_sets_attach_url(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        '[backends.opencode]\nattach_url = "http://from-config:4096"\n',
        monkeypatch,
    )
    model, effort, cmd, binary, prefix, attach_url = runner._resolve_backend_defaults(
        "opencode",
        model=None,
        effort=None,
        command_template=None,
        binary=None,
        terminal_prefix=None,
        attach_url=None,
    )
    assert attach_url == "http://from-config:4096"


def test_resolve_only_cli_flag_sets_attach_url(tmp_path, monkeypatch):
    # No [backends.opencode] in config.
    _write_config(tmp_path, "", monkeypatch)
    *_, attach_url = runner._resolve_backend_defaults(
        "opencode",
        model=None,
        effort=None,
        command_template=None,
        binary=None,
        terminal_prefix=None,
        attach_url="http://from-cli:4096",
    )
    assert attach_url == "http://from-cli:4096"


def test_resolve_cli_wins_over_config(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        '[backends.opencode]\nattach_url = "http://from-config:4096"\n',
        monkeypatch,
    )
    *_, attach_url = runner._resolve_backend_defaults(
        "opencode",
        model=None,
        effort=None,
        command_template=None,
        binary=None,
        terminal_prefix=None,
        attach_url="http://from-cli:4096",
    )
    assert attach_url == "http://from-cli:4096"


def test_resolve_neither_set_returns_none(tmp_path, monkeypatch):
    _write_config(tmp_path, "", monkeypatch)
    *_, attach_url = runner._resolve_backend_defaults(
        "opencode",
        model=None,
        effort=None,
        command_template=None,
        binary=None,
        terminal_prefix=None,
        attach_url=None,
    )
    assert attach_url is None


def test_resolve_empty_string_cli_falls_through_to_config(tmp_path, monkeypatch):
    """Empty-string CLI flag is treated as unset, so config.toml fills in."""
    _write_config(
        tmp_path,
        '[backends.opencode]\nattach_url = "http://from-config:4096"\n',
        monkeypatch,
    )
    *_, attach_url = runner._resolve_backend_defaults(
        "opencode",
        model=None,
        effort=None,
        command_template=None,
        binary=None,
        terminal_prefix=None,
        attach_url="",
    )
    assert attach_url == "http://from-config:4096"


def test_resolve_empty_string_config_is_treated_as_unset(tmp_path, monkeypatch):
    """Empty-string in config.toml → not used; resolves to None when CLI is also empty."""
    _write_config(
        tmp_path,
        '[backends.opencode]\nattach_url = ""\n',
        monkeypatch,
    )
    *_, attach_url = runner._resolve_backend_defaults(
        "opencode",
        model=None,
        effort=None,
        command_template=None,
        binary=None,
        terminal_prefix=None,
        attach_url=None,
    )
    assert attach_url is None
