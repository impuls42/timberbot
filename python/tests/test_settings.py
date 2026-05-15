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
        "terminal": "wt -d {cwd} --",
        "pythonCommand": "python3",
        "agentBinary": "claude",
        "agentModel": "claude-sonnet-4-6",
        "agentEffort": "medium",
        "agentCommandTemplate": "{skill}",
        "agentAllowlistEnabled": False,
        "agentAllowedBinaries": ["opencode"],
    }))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        s = settings.load_mod_settings(p)
    assert s == {"httpPort": 8085}


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


def test_resolve_endpoint_prefers_explicit_args():
    host, port = settings.resolve_endpoint("10.0.0.1", 1234, settings={"httpHost": "x", "httpPort": 9})
    assert (host, port) == ("10.0.0.1", 1234)


def test_resolve_endpoint_falls_back_to_settings():
    host, port = settings.resolve_endpoint(settings={"httpHost": "x", "httpPort": 9})
    assert (host, port) == ("x", 9)


def test_resolve_endpoint_uses_defaults_when_settings_empty():
    host, port = settings.resolve_endpoint(settings={})
    assert (host, port) == ("127.0.0.1", 8085)
