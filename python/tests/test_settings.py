"""Unit tests for timberbot.settings."""
from __future__ import annotations

import json
import warnings

import pytest

from timberbot import settings


@pytest.fixture(autouse=True)
def _reset_warned_keys():
    settings._warned_keys.clear()
    yield
    settings._warned_keys.clear()


def test_load_mod_settings_returns_empty_when_missing(tmp_path):
    assert settings.load_mod_settings(tmp_path / "missing.json") == {}


def test_load_mod_settings_returns_parsed_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"httpPort": 9090, "httpHost": "1.2.3.4"}))
    s = settings.load_mod_settings(p)
    assert s == {"httpPort": 9090, "httpHost": "1.2.3.4"}


def test_load_mod_settings_returns_empty_on_garbage(tmp_path):
    (tmp_path / "settings.json").write_text("not json")
    assert settings.load_mod_settings(tmp_path / "settings.json") == {}


def test_load_mod_settings_strips_deprecated_keys(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "httpPort": 8085,
        "agentBinary": "claude",
        "terminal": "wt -d {cwd} --",
        "pythonCommand": "python3",
        "agentModel": "claude-sonnet-4-6",
        "agentEffort": "medium",
        "agentCommandTemplate": "{skill}",
        "agentAllowlistEnabled": False,
        "agentAllowedBinaries": ["opencode"],
    }))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        s = settings.load_mod_settings(p)
    # agentBinary is the storage for the still-active Backend dropdown,
    # so it must survive the strip even though similarly-named keys go.
    assert s == {"httpPort": 8085, "agentBinary": "claude"}


def test_load_mod_settings_emits_deprecation_warning(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"httpPort": 8085, "terminal": "wt -d {cwd} --"}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        settings.load_mod_settings(p)
    matches = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(matches) == 1
    assert "terminal" in str(matches[0].message)


def test_load_mod_settings_warns_only_once_per_key(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"terminal": "x"}))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        settings.load_mod_settings(p)
        settings.load_mod_settings(p)
    deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deps) == 1


def test_resolve_endpoint_prefers_explicit_args(monkeypatch):
    monkeypatch.setenv("TBOT_HOST", "ignored.example")
    monkeypatch.setenv("TBOT_PORT", "9999")
    host, port = settings.resolve_endpoint(
        "10.0.0.1", 1234,
        settings={"httpHost": "x", "httpPort": 9},
        user_config={"host": "y", "port": 7},
    )
    assert (host, port) == ("10.0.0.1", 1234)


def test_resolve_endpoint_uses_env_when_no_explicit(monkeypatch):
    monkeypatch.setenv("TBOT_HOST", "10.0.0.2")
    monkeypatch.setenv("TBOT_PORT", "4321")
    host, port = settings.resolve_endpoint(
        settings={"httpHost": "x", "httpPort": 9},
        user_config={"host": "y", "port": 7},
    )
    assert (host, port) == ("10.0.0.2", 4321)


def test_resolve_endpoint_uses_user_config_when_no_env(monkeypatch):
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(
        settings={"httpHost": "x", "httpPort": 9},
        user_config={"host": "y", "port": 7},
    )
    assert (host, port) == ("y", 7)


def test_resolve_endpoint_falls_back_to_mod_settings(monkeypatch):
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(
        settings={"httpHost": "x", "httpPort": 9},
        user_config={},
    )
    assert (host, port) == ("x", 9)


def test_resolve_endpoint_uses_defaults_when_everything_empty(monkeypatch):
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(settings={}, user_config={})
    assert (host, port) == ("127.0.0.1", 8085)


def test_resolve_endpoint_ignores_malformed_tbot_port(monkeypatch):
    monkeypatch.setenv("TBOT_PORT", "not-a-number")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        host, port = settings.resolve_endpoint(settings={"httpPort": 9}, user_config={})
    assert port == 9
    assert any("TBOT_PORT" in str(w.message) for w in caught)
    assert host == "127.0.0.1"


def test_resolve_endpoint_env_partial_override(monkeypatch):
    """TBOT_HOST without TBOT_PORT still falls through cleanly for the missing field."""
    monkeypatch.setenv("TBOT_HOST", "10.0.0.3")
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(
        settings={"httpPort": 9}, user_config={"port": 7},
    )
    assert (host, port) == ("10.0.0.3", 7)
