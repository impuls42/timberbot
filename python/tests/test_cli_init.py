"""Tests for `tbot init`."""
from __future__ import annotations

from tbot.cli.commands import init_cmd


def test_init_materializes_all_prompts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    rc = init_cmd.run([])
    assert rc == 0
    prompts_dir = tmp_path / "agent_prompts"
    assert (prompts_dir / "timberbot.md").exists()
    assert (prompts_dir / "wirer.md").exists()
    assert (prompts_dir / "auditor.md").exists()
    assert (prompts_dir / "scout.md").exists()
    assert (prompts_dir / "beaver-developer.md").exists()


def test_init_is_idempotent_without_force(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    init_cmd.run([])
    # user edits the file
    target = tmp_path / "agent_prompts" / "timberbot.md"
    target.write_text("USER EDITED", encoding="utf-8")
    init_cmd.run([])
    assert target.read_text(encoding="utf-8") == "USER EDITED"


def test_init_force_overwrites(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    init_cmd.run([])
    target = tmp_path / "agent_prompts" / "timberbot.md"
    target.write_text("USER EDITED", encoding="utf-8")
    init_cmd.run(["--force"])
    assert target.read_text(encoding="utf-8") != "USER EDITED"


def test_init_list_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    rc = init_cmd.run(["--list"])
    assert rc == 0
    # no files written
    assert not (tmp_path / "agent_prompts" / "timberbot.md").exists()
