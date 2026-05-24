"""Tests for the `tbot serve` startup probe.

The probe was fail-fast (single `client._get_json("/api/ping")` call → raise
`ModUnreachableError`). It now defaults to wait-with-backoff so the player
can launch `tbot serve` and the game in either order. `--no-wait` (i.e.
`ServeConfig.wait_for_mod=False`) keeps the legacy fail-fast behaviour for
scripts and CI.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import requests

from timberbot.user_api.serve import (
    ModUnreachableError,
    ServeConfig,
    _probe_mod_until_reachable,
)


def _cfg(**overrides) -> ServeConfig:
    return ServeConfig(
        host="127.0.0.1", port=8085, ws_port=8086,
        telegram_token="fake-token",
        **overrides,
    )


def test_default_config_waits_for_mod():
    """`wait_for_mod` defaults True so `tbot serve` is launch-order-agnostic."""
    assert _cfg().wait_for_mod is True


def test_no_wait_raises_on_first_failure():
    """`wait_for_mod=False` keeps the legacy fail-fast behaviour."""
    cfg = _cfg(wait_for_mod=False)
    client = MagicMock()
    client._get_json.side_effect = requests.ConnectionError("refused")

    with pytest.raises(ModUnreachableError) as exc_info:
        asyncio.run(_probe_mod_until_reachable(client, cfg))
    msg = str(exc_info.value)
    assert "127.0.0.1:8085" in msg
    # Error message points the user at the wait-by-default escape hatch.
    assert "--no-wait" in msg
    assert client._get_json.call_count == 1


def test_no_wait_returns_quietly_when_mod_is_up():
    """First successful probe returns; no retry, no exception."""
    cfg = _cfg(wait_for_mod=False)
    client = MagicMock()
    client._get_json.return_value = {"status": "ok"}

    asyncio.run(_probe_mod_until_reachable(client, cfg))
    assert client._get_json.call_count == 1


def test_wait_retries_until_mod_comes_up(monkeypatch):
    """In wait-mode the probe retries until `_get_json` succeeds."""
    cfg = _cfg()  # wait_for_mod=True
    client = MagicMock()
    # 3 failures, then success.
    client._get_json.side_effect = [
        requests.ConnectionError("refused"),
        requests.ConnectionError("refused"),
        requests.Timeout("timed out"),
        {"status": "ok"},
    ]

    # Skip the real backoff sleeps so the test is fast. Capture the real
    # `asyncio.sleep` first so the patched version doesn't recurse into itself.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "timberbot.user_api.serve.asyncio.sleep",
        lambda _delay: _real_sleep(0),
    )

    asyncio.run(_probe_mod_until_reachable(client, cfg))
    assert client._get_json.call_count == 4


def test_wait_returns_immediately_when_mod_already_up():
    """No sleep, no log spam, just exit the first iteration."""
    cfg = _cfg()
    client = MagicMock()
    client._get_json.return_value = {"status": "ok"}

    asyncio.run(_probe_mod_until_reachable(client, cfg))
    assert client._get_json.call_count == 1


def test_wait_logs_waiting_message_once(caplog, monkeypatch):
    """The first retry logs a clear 'waiting for mod' line so an operator
    running `tbot serve` before the game knows what's happening; subsequent
    retries don't re-log at INFO to avoid spam."""
    import logging
    caplog.set_level(logging.INFO, logger="timberbot.user_api")

    cfg = _cfg()
    client = MagicMock()
    client._get_json.side_effect = [
        requests.ConnectionError("refused"),
        requests.ConnectionError("refused"),
        {"status": "ok"},
    ]
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "timberbot.user_api.serve.asyncio.sleep",
        lambda _delay: _real_sleep(0),
    )

    asyncio.run(_probe_mod_until_reachable(client, cfg))

    info_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    waiting_messages = [m for m in info_messages if "waiting for mod" in m]
    assert len(waiting_messages) == 1, (
        f"expected exactly one 'waiting for mod' INFO line; got {info_messages}"
    )
    # And a final "mod reachable" line once we get through.
    reachable_messages = [m for m in info_messages if "mod reachable" in m]
    assert len(reachable_messages) == 1


def test_wait_propagates_unexpected_exception(monkeypatch):
    """Non-connection exceptions (e.g. malformed JSON, programming errors)
    must NOT be swallowed by the retry loop — they bubble out so the bug
    surfaces instead of looping forever on a non-recoverable error."""
    cfg = _cfg()
    client = MagicMock()
    client._get_json.side_effect = ValueError("totally unexpected")

    _real_sleep = asyncio.sleep
    monkeypatch.setattr(
        "timberbot.user_api.serve.asyncio.sleep",
        lambda _delay: _real_sleep(0),
    )

    with pytest.raises(ValueError, match="totally unexpected"):
        asyncio.run(_probe_mod_until_reachable(client, cfg))


def test_serve_cli_threads_no_wait_into_config(monkeypatch):
    """`tbot serve --no-wait` flips `ServeConfig.wait_for_mod=False`."""
    import timberbot.cli.commands.serve as serve_mod

    captured: dict[str, ServeConfig] = {}

    async def _fake_run_serve(cfg):
        captured["cfg"] = cfg
        return 0

    monkeypatch.setattr(serve_mod, "serve_config", lambda: {})
    monkeypatch.setattr(serve_mod, "serve_telegram_config", lambda: {})
    monkeypatch.setattr(serve_mod, "resolve_telegram_token", lambda *_: "fake-token")
    monkeypatch.setattr(serve_mod, "resolve_endpoint", lambda *_a, **_kw: ("127.0.0.1", 8085))
    monkeypatch.setattr(serve_mod, "resolve_auth_token", lambda *_a, **_kw: None)
    monkeypatch.setattr(serve_mod, "resolve_ws_port", lambda *_: 8086)

    # Patch run_serve where serve.py imports it (inside its try-import block).
    # Easiest: patch user_api.serve.run_serve and let the import resolve.
    monkeypatch.setattr(
        "timberbot.user_api.serve.run_serve", _fake_run_serve,
    )

    # Default → wait_for_mod=True.
    serve_mod.serve()
    assert captured["cfg"].wait_for_mod is True

    captured.clear()

    # --no-wait flips it to False.
    serve_mod.serve(no_wait=True)
    assert captured["cfg"].wait_for_mod is False
