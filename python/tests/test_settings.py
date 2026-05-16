"""Unit tests for timberbot.settings.

The client-side `settings.json` fallback was removed in #43 PR 2; these tests
exercise the 3-tier precedence chain (explicit → env → user_config → default).
"""
from __future__ import annotations

import warnings

from timberbot import settings


def test_resolve_endpoint_prefers_explicit_args(monkeypatch):
    monkeypatch.setenv("TBOT_HOST", "ignored.example")
    monkeypatch.setenv("TBOT_PORT", "9999")
    host, port = settings.resolve_endpoint(
        "10.0.0.1", 1234,
        user_config={"host": "y", "port": 7},
    )
    assert (host, port) == ("10.0.0.1", 1234)


def test_resolve_endpoint_uses_env_when_no_explicit(monkeypatch):
    monkeypatch.setenv("TBOT_HOST", "10.0.0.2")
    monkeypatch.setenv("TBOT_PORT", "4321")
    host, port = settings.resolve_endpoint(user_config={"host": "y", "port": 7})
    assert (host, port) == ("10.0.0.2", 4321)


def test_resolve_endpoint_uses_user_config_when_no_env(monkeypatch):
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(user_config={"host": "y", "port": 7})
    assert (host, port) == ("y", 7)


def test_resolve_endpoint_uses_defaults_when_everything_empty(monkeypatch):
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(user_config={})
    assert (host, port) == ("127.0.0.1", 8085)


def test_resolve_endpoint_ignores_malformed_tbot_port(monkeypatch):
    """Malformed TBOT_PORT falls through to user_config / default, NOT to mod settings."""
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.setenv("TBOT_PORT", "not-a-number")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        host, port = settings.resolve_endpoint(user_config={})
    assert (host, port) == ("127.0.0.1", 8085)
    assert any("TBOT_PORT" in str(w.message) for w in caught)


def test_resolve_endpoint_env_partial_override(monkeypatch):
    """TBOT_HOST without TBOT_PORT still falls through cleanly for the missing field."""
    monkeypatch.setenv("TBOT_HOST", "10.0.0.3")
    monkeypatch.delenv("TBOT_PORT", raising=False)
    host, port = settings.resolve_endpoint(user_config={"port": 7})
    assert (host, port) == ("10.0.0.3", 7)


def test_resolve_endpoint_no_settings_param(monkeypatch):
    """Regression: the `settings=` kwarg was removed in #43 PR 2.

    A stale mod-side settings.json with `httpHost`/`httpPort` must not leak into
    client endpoint resolution, even if someone tried to pass it. The signature
    no longer accepts the kwarg, so the call raises TypeError.
    """
    monkeypatch.delenv("TBOT_HOST", raising=False)
    monkeypatch.delenv("TBOT_PORT", raising=False)
    import pytest
    with pytest.raises(TypeError):
        settings.resolve_endpoint(  # type: ignore[call-arg]
            settings={"httpHost": "ignored", "httpPort": 9999},
            user_config={},
        )


# ---------------------------------------------------------------------------
# Auth token resolution (unchanged behaviour, kept as regression coverage)
# ---------------------------------------------------------------------------


def test_resolve_auth_token_prefers_explicit_arg(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "from-env")
    token = settings.resolve_auth_token("from-arg", user_config={"auth_token": "from-cfg"})
    assert token == "from-arg"


def test_resolve_auth_token_uses_env_when_no_arg(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "from-env")
    token = settings.resolve_auth_token(user_config={"auth_token": "from-cfg"})
    assert token == "from-env"


def test_resolve_auth_token_uses_user_config_when_no_env(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    token = settings.resolve_auth_token(user_config={"auth_token": "from-cfg"})
    assert token == "from-cfg"


def test_resolve_auth_token_none_when_unset(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    assert settings.resolve_auth_token(user_config={}) is None


def test_resolve_auth_token_treats_whitespace_as_unset(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "   ")
    token = settings.resolve_auth_token("  ", user_config={"auth_token": "good"})
    assert token == "good"
