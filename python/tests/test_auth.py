"""Tests for bearer-token threading + precedence resolution."""
from __future__ import annotations

import pytest

pytest.importorskip("pytest_httpserver")

from timberbot.api.client import TimberbotClient  # noqa: E402
from timberbot.api.exceptions import AuthenticationError, TimberbotError  # noqa: E402
from timberbot.settings import resolve_auth_token  # noqa: E402

# ---------------------------------------------------------------------------
# resolve_auth_token precedence
# ---------------------------------------------------------------------------


def test_resolve_prefers_explicit_arg(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "from-env")
    token = resolve_auth_token("from-arg", user_config={"auth_token": "from-config"})
    assert token == "from-arg"


def test_resolve_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "from-env")
    token = resolve_auth_token(None, user_config={"auth_token": "from-config"})
    assert token == "from-env"


def test_resolve_falls_back_to_user_config(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    token = resolve_auth_token(None, user_config={"auth_token": "from-config"})
    assert token == "from-config"


def test_resolve_returns_none_when_nothing_set(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    assert resolve_auth_token(None, user_config={}) is None


def test_resolve_empty_string_arg_falls_through(monkeypatch):
    """Empty / whitespace explicit arg is the same as unset — the chain advances."""
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "from-env")
    assert resolve_auth_token("", user_config={}) == "from-env"
    assert resolve_auth_token("   ", user_config={}) == "from-env"


def test_resolve_empty_env_falls_through(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "")
    token = resolve_auth_token(None, user_config={"auth_token": "from-config"})
    assert token == "from-config"


def test_resolve_whitespace_env_falls_through(monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "   ")
    token = resolve_auth_token(None, user_config={"auth_token": "from-config"})
    assert token == "from-config"


def test_resolve_strips_whitespace(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    assert resolve_auth_token("  padded  ", user_config={}) == "padded"


def test_resolve_ignores_non_string_config(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    # A misconfigured TOML (e.g. `auth_token = 42`) must not crash the
    # resolver — just fall through to None.
    assert resolve_auth_token(None, user_config={"auth_token": 42}) is None
    assert resolve_auth_token(None, user_config={"auth_token": None}) is None


def test_resolve_ignores_empty_string_config(monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    assert resolve_auth_token(None, user_config={"auth_token": ""}) is None
    assert resolve_auth_token(None, user_config={"auth_token": "   "}) is None


# ---------------------------------------------------------------------------
# TimberbotClient header threading
# ---------------------------------------------------------------------------


def test_client_threads_authorization_header(httpserver, monkeypatch):
    """Constructor `auth_token` lands on the Session as a Bearer header on every request."""
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    httpserver.expect_request(
        "/api/ping",
        headers={"Authorization": "Bearer my-secret"},
    ).respond_with_json({"status": "ok", "ready": True, "openapiVersion": "1.0.0"})

    client = TimberbotClient(
        host=httpserver.host, port=httpserver.port, auth_token="my-secret",
    )
    assert client.ping() is True
    # The expectation above only matches when the header is present; if it
    # weren't, pytest-httpserver would 500 and ping() would still report
    # False from the JSON-decode failure path. The strict assert wins.


def test_client_no_header_when_unset(httpserver, monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    httpserver.expect_request("/api/ping").respond_with_json(
        {"status": "ok", "ready": True, "openapiVersion": "1.0.0"}
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port)
    assert "Authorization" not in client.s.headers
    assert client.ping() is True


def test_client_reads_env_token(httpserver, monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "env-token")
    httpserver.expect_request(
        "/api/ping",
        headers={"Authorization": "Bearer env-token"},
    ).respond_with_json({"status": "ok", "ready": True, "openapiVersion": "1.0.0"})
    client = TimberbotClient(host=httpserver.host, port=httpserver.port)
    assert client.ping() is True


def test_client_arg_overrides_env(httpserver, monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "env-token")
    httpserver.expect_request(
        "/api/ping",
        headers={"Authorization": "Bearer ctor-token"},
    ).respond_with_json({"status": "ok", "ready": True, "openapiVersion": "1.0.0"})
    client = TimberbotClient(
        host=httpserver.host, port=httpserver.port, auth_token="ctor-token",
    )
    assert client.ping() is True


def test_client_empty_token_arg_falls_through_to_env(httpserver, monkeypatch):
    monkeypatch.setenv("TBOT_AUTH_TOKEN", "env-token")
    httpserver.expect_request(
        "/api/ping",
        headers={"Authorization": "Bearer env-token"},
    ).respond_with_json({"status": "ok", "ready": True, "openapiVersion": "1.0.0"})
    client = TimberbotClient(
        host=httpserver.host, port=httpserver.port, auth_token="",
    )
    assert client.ping() is True


def test_client_threads_header_on_post(httpserver, monkeypatch):
    """Auth must apply to POSTs (writes) too, not just GETs."""
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    httpserver.expect_request(
        "/api/speed",
        method="POST",
        headers={"Authorization": "Bearer write-token"},
    ).respond_with_json({"status": "ok"})

    client = TimberbotClient(
        host=httpserver.host, port=httpserver.port, auth_token="write-token",
    )
    result = client.set_speed(2)
    assert result == {"status": "ok"}


# ---------------------------------------------------------------------------
# 401 handling: 401 must surface as AuthenticationError (subclass of
# TimberbotError), never as a raw requests.HTTPError. Callers can catch
# TimberbotError to handle all API failures uniformly.
# ---------------------------------------------------------------------------


def test_client_401_get_raises_authentication_error(httpserver, monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    httpserver.expect_request("/api/summary").respond_with_json(
        {"error": "unauthorized: missing or invalid bearer token"},
        status=401,
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port)
    with pytest.raises(AuthenticationError) as exc_info:
        client.summary()
    assert exc_info.value.code == "unauthorized"
    assert "missing or invalid bearer token" in exc_info.value.error
    # AuthenticationError is a TimberbotError subclass — generic handlers still work.
    assert isinstance(exc_info.value, TimberbotError)


def test_client_401_post_raises_authentication_error(httpserver, monkeypatch):
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    httpserver.expect_request("/api/speed", method="POST").respond_with_json(
        {"error": "unauthorized: missing or invalid bearer token"},
        status=401,
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port)
    with pytest.raises(AuthenticationError):
        client.set_speed(2)


def test_client_401_with_unparseable_body_synthesises_error(httpserver, monkeypatch):
    """If the server returns a non-JSON 401 body we still raise AuthenticationError."""
    monkeypatch.delenv("TBOT_AUTH_TOKEN", raising=False)
    httpserver.expect_request("/api/summary").respond_with_data(
        "Unauthorized", status=401, content_type="text/plain",
    )
    client = TimberbotClient(host=httpserver.host, port=httpserver.port)
    with pytest.raises(AuthenticationError) as exc_info:
        client.summary()
    assert exc_info.value.code == "unauthorized"
