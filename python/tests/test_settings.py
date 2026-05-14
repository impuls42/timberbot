"""Unit tests for tbot.settings."""
from __future__ import annotations

import json

from tbot import settings


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


def test_resolve_endpoint_prefers_explicit_args():
    host, port = settings.resolve_endpoint("10.0.0.1", 1234, settings={"httpHost": "x", "httpPort": 9})
    assert (host, port) == ("10.0.0.1", 1234)


def test_resolve_endpoint_falls_back_to_settings():
    host, port = settings.resolve_endpoint(settings={"httpHost": "x", "httpPort": 9})
    assert (host, port) == ("x", 9)


def test_resolve_endpoint_uses_defaults_when_settings_empty():
    host, port = settings.resolve_endpoint(settings={})
    assert (host, port) == ("127.0.0.1", 8085)
