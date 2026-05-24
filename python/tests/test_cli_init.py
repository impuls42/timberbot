"""Tests for `tbot init`."""
from __future__ import annotations

from timberbot.cli.commands.init_cmd import init


def test_init_materializes_all_prompts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    rc = init()
    assert rc == 0
    prompts_dir = tmp_path / "agent_prompts"
    assert (prompts_dir / "timberbot.md").exists()
    assert (prompts_dir / "wirer.md").exists()
    assert (prompts_dir / "auditor.md").exists()
    assert (prompts_dir / "scout.md").exists()
    # beaver-developer lives at repo-root `agents/`, not in the shipped
    # package — it targets working on this codebase itself.
    assert not (prompts_dir / "beaver-developer.md").exists()


def test_init_is_idempotent_without_force(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    init()
    target = tmp_path / "agent_prompts" / "timberbot.md"
    target.write_text("USER EDITED", encoding="utf-8")
    init()
    assert target.read_text(encoding="utf-8") == "USER EDITED"


def test_init_force_overwrites(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    init()
    target = tmp_path / "agent_prompts" / "timberbot.md"
    target.write_text("USER EDITED", encoding="utf-8")
    init(force=True)
    assert target.read_text(encoding="utf-8") != "USER EDITED"


def test_init_list_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    rc = init(list_only=True)
    assert rc == 0
    assert not (tmp_path / "agent_prompts" / "timberbot.md").exists()
