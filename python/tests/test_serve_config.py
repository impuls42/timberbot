"""Tests for serve_config, serve_telegram_config, and resolve_telegram_token."""
from __future__ import annotations

import pytest

import timberbot.cli.commands.serve as serve_mod
from timberbot.cli.commands.serve import resolve_telegram_token
from timberbot.user_config import reset_warning_cache, serve_config, serve_telegram_config


@pytest.fixture(autouse=True)
def _reset_warning_cache():
    reset_warning_cache()
    yield
    reset_warning_cache()


def test_serve_config_empty_when_missing():
    assert serve_config({}) == {}


def test_serve_config_returns_section():
    data = {"serve": {"backend": "claude", "mcp_port": 8091}}
    assert serve_config(data) == {"backend": "claude", "mcp_port": 8091}


def test_serve_config_empty_when_not_dict():
    data = {"serve": "not-a-table"}
    assert serve_config(data) == {}


def test_serve_telegram_config_empty_when_no_section():
    data = {"serve": {"backend": "claude"}}
    assert serve_telegram_config(data) == {}


def test_serve_telegram_config_returns_nested():
    data = {"serve": {"telegram": {"token": "123:ABC"}}}
    assert serve_telegram_config(data) == {"token": "123:ABC"}


def test_serve_telegram_config_empty_on_empty_input():
    assert serve_telegram_config({}) == {}


def test_resolve_telegram_token_explicit_wins():
    assert resolve_telegram_token("explicit-token") == "explicit-token"


def test_resolve_telegram_token_env_var(monkeypatch):
    monkeypatch.setenv("TBOT_TELEGRAM_TOKEN", "env-token")
    assert resolve_telegram_token(None) == "env-token"


def test_resolve_telegram_token_missing_exits(monkeypatch):
    monkeypatch.delenv("TBOT_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setattr(serve_mod, "serve_telegram_config", lambda: {})
    with pytest.raises(SystemExit):
        resolve_telegram_token(None)


def test_parse_defaults_to_none():
    from timberbot.cli.commands.serve import _parse
    ns = _parse([])
    assert ns.backend is None
    assert ns.model is None
    assert ns.acp_binary is None
    assert ns.telegram_token is None
    assert ns.mcp_port is None
    assert ns.mcp_host is None
    assert ns.ws_port is None


def test_parse_rejects_unknown_backend():
    from timberbot.cli.commands.serve import _parse
    with pytest.raises(SystemExit):
        _parse(["--backend", "gpt-4"])


def test_parse_accepts_known_backends():
    from timberbot.cli.commands.serve import _parse
    assert _parse(["--backend", "claude"]).backend == "claude"
    assert _parse(["--backend", "opencode"]).backend == "opencode"


def test_parse_acp_binary_explicit():
    from timberbot.cli.commands.serve import _parse
    ns = _parse(["--acp-binary", "/opt/bin/claude"])
    assert ns.acp_binary == "/opt/bin/claude"
