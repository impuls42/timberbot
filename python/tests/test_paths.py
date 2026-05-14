"""Unit tests for tbot.paths."""
from __future__ import annotations

from pathlib import Path

from tbot import paths


def test_documents_dir_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert paths.documents_dir() == tmp_path / "Documents" / "Timberborn"


def test_mod_dir_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    md = paths.mod_dir()
    assert md == tmp_path / "Documents" / "Timberborn" / "Mods" / "Timberbot"
    assert paths.settings_path() == md / "settings.json"
    assert paths.memory_base() == md / "memory"
    assert paths.saves_dir() == tmp_path / "Documents" / "Timberborn" / "Saves"


def test_sanitize_name_strips_filesystem_unsafe_chars():
    assert paths.sanitize_name("My/Castle") == "My_Castle"
    assert paths.sanitize_name('hello"world') == "hello_world"
    assert paths.sanitize_name("a<b>c|d?e*f") == "a_b_c_d_e_f"
    assert paths.sanitize_name("  ") == "unknown"
    assert paths.sanitize_name("") == "unknown"
    assert paths.sanitize_name("simple") == "simple"
