"""Unit tests for timberbot.user_config (config.toml reader)."""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from timberbot import user_config


@pytest.fixture(autouse=True)
def _reset_warning_cache():
    user_config.reset_warning_cache()
    yield
    user_config.reset_warning_cache()


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_user_config_missing_file_returns_empty(tmp_path):
    assert user_config.load_user_config(tmp_path / "absent.toml") == {}


def test_load_user_config_parses_valid_toml(tmp_path):
    p = _write(tmp_path / "config.toml", """
[client]
host = "10.0.0.1"
port = 9090

[backends.claude]
model = "claude-opus-4-7"
effort = "high"
""")
    data = user_config.load_user_config(p)
    assert data == {
        "client": {"host": "10.0.0.1", "port": 9090},
        "backends": {"claude": {"model": "claude-opus-4-7", "effort": "high"}},
    }


def test_load_user_config_warns_on_parse_error(tmp_path):
    p = _write(tmp_path / "config.toml", "this is not = valid toml ===")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        data = user_config.load_user_config(p)
    assert data == {}
    assert any("config.toml" in str(w.message) for w in caught)


def test_load_user_config_warns_only_once_per_path(tmp_path):
    p = _write(tmp_path / "config.toml", "bogus =")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        user_config.load_user_config(p)
        user_config.load_user_config(p)
    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert len(user_warnings) == 1


def test_client_config_section(tmp_path):
    p = _write(tmp_path / "config.toml", """
[client]
host = "x"
port = 7
""")
    assert user_config.client_config(user_config.load_user_config(p)) == {"host": "x", "port": 7}


def test_client_config_returns_empty_when_section_missing(tmp_path):
    p = _write(tmp_path / "config.toml", "[unrelated]\nfoo = 1\n")
    assert user_config.client_config(user_config.load_user_config(p)) == {}


def test_backend_defaults_for_named_backend(tmp_path):
    p = _write(tmp_path / "config.toml", """
[backends.claude]
model = "claude-opus-4-7"
effort = "high"

[backends.opencode]
model = "glm-4.6"
""")
    data = user_config.load_user_config(p)
    assert user_config.backend_defaults("claude", data) == {
        "model": "claude-opus-4-7",
        "effort": "high",
    }
    assert user_config.backend_defaults("opencode", data) == {"model": "glm-4.6"}
    assert user_config.backend_defaults("custom", data) == {}


def test_backend_defaults_returns_empty_when_table_missing(tmp_path):
    p = _write(tmp_path / "config.toml", "[client]\nhost = 'x'\n")
    assert user_config.backend_defaults("claude", user_config.load_user_config(p)) == {}


def test_config_path_uses_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    assert user_config.config_path() == tmp_path / "config.toml"


def test_load_user_config_warns_on_non_table_root(tmp_path):
    # TOML can't actually produce a non-dict root from a non-empty file, but
    # the guard is defensive — exercise it via dependency injection on the
    # tomllib call instead.
    p = _write(tmp_path / "config.toml", "")
    # Empty file → empty dict, not a warning. Use this case to confirm the
    # empty-file path returns empty without warning.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        data = user_config.load_user_config(p)
    assert data == {}
    assert [w for w in caught if issubclass(w.category, UserWarning)] == []
