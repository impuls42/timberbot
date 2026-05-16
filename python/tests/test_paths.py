"""Unit tests for timberbot.paths."""
from __future__ import annotations

import pytest

from timberbot import paths


@pytest.fixture(autouse=True)
def _reset_paths_cache():
    paths.reset_cache()
    yield
    paths.reset_cache()


def test_documents_dir_from_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TBOT_DOCUMENTS_DIR", str(tmp_path / "Timberborn"))
    assert paths.documents_dir() == tmp_path / "Timberborn"


def test_mod_dir_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("TBOT_DOCUMENTS_DIR", str(tmp_path / "Timberborn"))
    md = paths.mod_dir()
    assert md == tmp_path / "Timberborn" / "Mods" / "Timberbot"
    assert paths.memory_base() == md / "memory"
    assert paths.saves_dir() == tmp_path / "Timberborn" / "Saves"


def test_mod_dir_override_pins_path(tmp_path):
    paths.set_mod_dir_override(tmp_path / "custom-mod")
    assert paths.mod_dir() == tmp_path / "custom-mod"
    assert paths.memory_base() == tmp_path / "custom-mod" / "memory"


def test_mod_dir_from_env_var(monkeypatch, tmp_path):
    """TBOT_MOD_DIR takes precedence over the documents-dir-derived default."""
    monkeypatch.setenv("TBOT_DOCUMENTS_DIR", str(tmp_path / "Timberborn"))
    monkeypatch.setenv("TBOT_MOD_DIR", str(tmp_path / "alt-mod"))
    assert paths.mod_dir() == tmp_path / "alt-mod"


def test_explicit_override_beats_env_var(monkeypatch, tmp_path):
    """`set_mod_dir_override` wins over TBOT_MOD_DIR (CLI flag tier)."""
    monkeypatch.setenv("TBOT_DOCUMENTS_DIR", str(tmp_path / "Timberborn"))
    monkeypatch.setenv("TBOT_MOD_DIR", str(tmp_path / "from-env"))
    paths.set_mod_dir_override(tmp_path / "from-cli")
    assert paths.mod_dir() == tmp_path / "from-cli"


def test_sanitize_name_strips_filesystem_unsafe_chars():
    assert paths.sanitize_name("My/Castle") == "My_Castle"
    assert paths.sanitize_name('hello"world') == "hello_world"
    assert paths.sanitize_name("a<b>c|d?e*f") == "a_b_c_d_e_f"
    assert paths.sanitize_name("  ") == "unknown"
    assert paths.sanitize_name("") == "unknown"
    assert paths.sanitize_name("simple") == "simple"
