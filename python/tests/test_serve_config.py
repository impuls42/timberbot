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


def test_serve_signature_exposes_expected_flags():
    """Fire reflects `serve(...)`'s parameters as CLI flags. Lock the surface."""
    import inspect

    params = inspect.signature(serve_mod.serve).parameters
    for name in (
        "backend", "model", "acp_binary", "telegram_token",
        "mcp_port", "mcp_host", "ws_port",
    ):
        assert name in params, f"missing CLI flag: --{name.replace('_', '-')}"
        assert params[name].default is None


def test_serve_rejects_unknown_backend(monkeypatch, capsys):
    _stub_serve(monkeypatch, {})
    rc = serve_mod.serve(backend="gpt-4")
    assert rc == 1
    assert "unknown backend" in capsys.readouterr().err


def test_serve_accepts_known_backends(monkeypatch):
    _stub_serve(monkeypatch, {})
    assert serve_mod.serve(backend="claude") == 0
    assert serve_mod.serve(backend="opencode") == 0


def test_serve_rejects_empty_acp_binary(monkeypatch, capsys):
    _stub_serve(monkeypatch, {})
    rc = serve_mod.serve(acp_binary="")
    assert rc == 1
    assert "--acp-binary must not be empty" in capsys.readouterr().err


# -- allowlist config validation ------------------------------------------

def _stub_serve(monkeypatch, tg_data: dict) -> None:
    """Minimal stub so `serve_mod.serve(...)` can reach the allowlist branch
    without spinning up the actual asyncio.run(run_serve(...)) flow."""
    monkeypatch.setattr(serve_mod, "serve_config", lambda: {})
    monkeypatch.setattr(serve_mod, "serve_telegram_config", lambda: tg_data)
    monkeypatch.setattr(serve_mod, "resolve_telegram_token", lambda *_: "fake-token")
    # `serve()` now passes global --host/--port/--auth-token through to the
    # resolvers; the stubs swallow whatever it forwards.
    monkeypatch.setattr(serve_mod, "resolve_endpoint", lambda *_a, **_kw: ("127.0.0.1", 8085))
    monkeypatch.setattr(serve_mod, "resolve_auth_token", lambda *_a, **_kw: None)
    monkeypatch.setattr(serve_mod, "resolve_ws_port", lambda *_: 8086)
    # Stop short of actually running the orchestrator — patch asyncio.run
    # so we exit cleanly after ServeConfig is built. Close the coroutine
    # so pytest doesn't emit a "never awaited" RuntimeWarning.
    def _fake_run(coro, *args, **kwargs):
        coro.close()
        return 0
    monkeypatch.setattr(serve_mod.asyncio, "run", _fake_run)


def test_serve_rejects_allowed_users_not_a_list(monkeypatch, capsys):
    _stub_serve(monkeypatch, {"allowed_users": 12345})
    rc = serve_mod.serve()
    assert rc == 1
    err = capsys.readouterr().err
    assert "must be a list of integers" in err
    assert "int" in err   # mentions the offending type


def test_serve_rejects_allowed_users_with_non_integer(monkeypatch, capsys):
    _stub_serve(monkeypatch, {"allowed_users": [123, "alice"]})
    rc = serve_mod.serve()
    assert rc == 1
    assert "must contain integers" in capsys.readouterr().err


def test_serve_accepts_empty_allowed_users(monkeypatch):
    _stub_serve(monkeypatch, {})
    rc = serve_mod.serve()
    assert rc == 0


def test_serve_accepts_valid_allowed_users(monkeypatch):
    _stub_serve(monkeypatch, {"allowed_users": [123, 456]})
    rc = serve_mod.serve()
    assert rc == 0
