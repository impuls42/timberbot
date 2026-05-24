"""Tests for `tbot listen` — the WebSocket event subscriber.

The HTTP-inbound implementation was removed as part of the heartbeat/webhook
→ WS cutover (issue #31). These tests cover the new behaviour:

  * event-frame passthrough (`type == 'event'`),
  * non-event frames dropped silently,
  * `--forward-to file://` and `--forward-to http://` (downstream mocked with
    `pytest-httpserver`),
  * `--pretty` and `--quiet` rendering,
  * reconnect after WS close,
  * auth token threaded into the WS upgrade headers,
  * argparse + registry wiring.

We mock the underlying aiohttp WS session so the suite stays hermetic; an
end-to-end check against a real `/api/ws` will land once the WS server
(unit #28) and typed client (unit #29) are merged.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("pytest_httpserver")

import aiohttp  # noqa: E402

from timberbot.cli.commands import listen as listen_cmd  # noqa: E402

# ---------------------------------------------------------------------------
# Fake aiohttp WS session / message plumbing.
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Stand-in for `aiohttp.WSMessage` carrying the bits `_consume` reads."""

    def __init__(self, type_: aiohttp.WSMsgType, data: Any = "") -> None:
        self.type = type_
        self.data = data


def _text(payload: dict[str, Any]) -> _FakeMessage:
    return _FakeMessage(aiohttp.WSMsgType.TEXT, json.dumps(payload))


_CLOSED = _FakeMessage(aiohttp.WSMsgType.CLOSED)


class _FakeWebSocket:
    """Async iterator over a fixed message list. Mimics `ClientWebSocketResponse`."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> _FakeMessage:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeWSContext:
    """`async with session.ws_connect(...)` returns one of these."""

    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWebSocket:
        return self._ws

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeSession:
    """Programmable replacement for `aiohttp.ClientSession`.

    `ws_connect` pops the next scripted outcome from `connect_plan` (either
    a list of `_FakeMessage`s for success, or an exception class to raise).

    `post` records the call for assertions. If `real_post_session` is
    supplied, the call is also forwarded to a live `aiohttp.ClientSession`
    so the end-to-end HTTP forward path actually hits a real socket — used
    by the pytest-httpserver test.
    """

    def __init__(
        self,
        connect_plan: list[Any],
        *,
        real_post_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.connect_plan = list(connect_plan)
        self.connect_calls: list[dict[str, Any]] = []
        self.post_calls: list[tuple[str, Any]] = []
        self._real_post_session = real_post_session

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def ws_connect(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        self.connect_calls.append({"url": url, "headers": dict(headers or {})})
        if not self.connect_plan:
            raise RuntimeError("FakeSession: no more connect outcomes scripted")
        outcome = self.connect_plan.pop(0)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome("scripted failure")
        return _FakeWSContext(_FakeWebSocket(outcome))

    def post(self, url: str, *, json: Any = None, timeout: float | None = None) -> Any:
        self.post_calls.append((url, json))
        if self._real_post_session is not None:
            return self._real_post_session.post(url, json=json, timeout=timeout)

        class _Resp:
            async def __aenter__(self_inner) -> _Resp:
                return self_inner

            async def __aexit__(self_inner, *exc: Any) -> None:
                return None

            async def read(self_inner) -> bytes:
                return b""

        return _Resp()


def _make_factory(session: _FakeSession):
    """Return a zero-arg callable that yields the same session each call.

    `subscribe` calls `session_factory()` once per connect attempt; production
    builds a fresh ClientSession each time. Tests reuse a single fake so
    `post_calls` accumulates across reconnects.
    """
    return lambda: session


async def _no_sleep(_seconds: float) -> None:
    """Skip the reconnect backoff so tests don't real-time-wait."""
    return None


# ---------------------------------------------------------------------------
# Fixtures / sample payloads.
# ---------------------------------------------------------------------------


SAMPLE_EVENT = {
    "type": "event",
    "event": "drought.start",
    "day": 45,
    "timestamp": 1711300000,
    "data": {"duration": 8},
}

OTHER_EVENT = {
    "type": "event",
    "event": "beaver.died",
    "day": 45,
    "timestamp": 1711300000,
    "data": None,
}

NON_EVENT_FRAME = {"type": "heartbeat", "ts": 1711300000}


# ---------------------------------------------------------------------------
# subscribe() behaviour.
# ---------------------------------------------------------------------------


def test_event_frames_passthrough_to_stdout(capsys):
    session = _FakeSession([[_text(SAMPLE_EVENT), _text(OTHER_EVENT), _CLOSED]])
    rc = asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    # Two raw-JSON lines (default mode).
    assert len(out) == 2
    assert json.loads(out[0])["event"] == "drought.start"
    assert json.loads(out[1])["event"] == "beaver.died"


def test_non_event_frames_dropped(capsys):
    session = _FakeSession([[_text(NON_EVENT_FRAME), _text(SAMPLE_EVENT), _CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "drought.start"


def test_pretty_mode_human_friendly(capsys):
    session = _FakeSession([[_text(SAMPLE_EVENT), _CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085, pretty=True,
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    out = capsys.readouterr().out
    assert "[day 45" in out
    assert "drought.start" in out
    # Raw JSON should NOT appear in pretty mode.
    assert '"event":' not in out and '"event": ' not in out


def test_quiet_suppresses_stdout(capsys, tmp_path):
    sink = tmp_path / "out.jsonl"
    session = _FakeSession([[_text(SAMPLE_EVENT), _CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        quiet=True, forward_to=f"file://{sink}",
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    out = capsys.readouterr().out
    assert out == ""
    # The file still receives the event.
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "drought.start"


def test_forward_to_file_writes_jsonl(tmp_path):
    sink = tmp_path / "events.jsonl"
    session = _FakeSession([[_text(SAMPLE_EVENT), _text(OTHER_EVENT), _CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        quiet=True, forward_to=f"file://{sink}",
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "drought.start"
    assert json.loads(lines[1])["event"] == "beaver.died"


def test_forward_to_bare_path_also_works(tmp_path):
    sink = tmp_path / "nested" / "events.jsonl"
    session = _FakeSession([[_text(SAMPLE_EVENT), _CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        quiet=True, forward_to=str(sink),
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    assert sink.exists()
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 1


def test_forward_to_http_posts_downstream(httpserver):
    received: list[Any] = []

    def _capture(request):
        received.append(request.get_json())
        return ("", 200, {})

    httpserver.expect_request("/sink", method="POST").respond_with_handler(_capture)
    downstream = httpserver.url_for("/sink")

    # Mock WS, real HTTP: `_FakeSession.post` delegates to a live
    # `aiohttp.ClientSession` so the forward path actually hits the
    # pytest-httpserver socket end-to-end.
    async def _run() -> _FakeSession:
        async with aiohttp.ClientSession() as real_http:
            session = _FakeSession(
                [[_text(SAMPLE_EVENT), _CLOSED]],
                real_post_session=real_http,
            )
            await listen_cmd.subscribe(
                host="127.0.0.1", ws_port=8085,
                forward_to=downstream, quiet=True,
                max_attempts=1, sleep=_no_sleep,
                session_factory=_make_factory(session),
            )
            return session

    session = asyncio.run(_run())

    assert received, "downstream never received the batch"
    # The wire shape is a 1-element batch so HTTP collectors that already
    # speak the old webhook batch shape don't need to special-case the
    # migration.
    assert received[0] == [SAMPLE_EVENT]
    # And the fake recorded the call as well, so the URL is observable.
    assert session.post_calls[0][0] == downstream


def test_reconnects_after_close(capsys):
    """Two scripted sessions: the first closes after one event, the second
    closes after another. `max_attempts=2` lets the loop run both and exit."""
    session = _FakeSession([
        [_text(SAMPLE_EVENT), _CLOSED],
        [_text(OTHER_EVENT), _CLOSED],
    ])
    rc = asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        max_attempts=2, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    assert rc == 0
    assert len(session.connect_calls) == 2
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["event"] for line in out] == ["drought.start", "beaver.died"]


def test_reconnects_after_client_error(capsys):
    """A connect that raises `ClientError` should backoff and retry, not crash."""
    session = _FakeSession([
        aiohttp.ClientError,
        [_text(SAMPLE_EVENT), _CLOSED],
    ])
    rc = asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085, quiet=True,
        max_attempts=2, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    assert rc == 0
    assert len(session.connect_calls) == 2


def test_auth_token_threaded_into_headers():
    session = _FakeSession([[_CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        auth_token="sekret", quiet=True,
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    assert session.connect_calls[0]["url"] == "ws://127.0.0.1:8085/api/ws"
    assert session.connect_calls[0]["headers"]["Authorization"] == "Bearer sekret"


def test_no_auth_header_when_token_missing():
    session = _FakeSession([[_CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085, quiet=True,
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    assert "Authorization" not in session.connect_calls[0]["headers"]


def test_malformed_frame_logged_but_connection_kept(capsys):
    """A bad TEXT frame should hit stderr but the connection keeps draining."""
    bad = _FakeMessage(aiohttp.WSMsgType.TEXT, "not-json{{{")
    session = _FakeSession([[bad, _text(SAMPLE_EVENT), _CLOSED]])
    asyncio.run(listen_cmd.subscribe(
        host="127.0.0.1", ws_port=8085,
        max_attempts=1, sleep=_no_sleep,
        session_factory=_make_factory(session),
    ))
    captured = capsys.readouterr()
    assert "malformed frame" in captured.err
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "drought.start"


# ---------------------------------------------------------------------------
# Argparse / wiring.
# ---------------------------------------------------------------------------


def test_fire_signature_accepts_new_flags():
    """Fire reflects `listen(...)`'s signature into CLI flags. Lock the surface.

    These are the flags users will see; the legacy argparse `_parse` is gone.
    """
    import inspect

    params = inspect.signature(listen_cmd.listen).parameters
    for name in ("pretty", "forward_to", "quiet", "ws_port", "host", "auth_token"):
        assert name in params, f"missing CLI flag: --{name.replace('_', '-')}"
    # `--port` (HTTP-inbound legacy) must not be exposed by the listen command.
    assert "port" not in params


def test_registered_on_tbot_class():
    """The Fire dispatcher reflects methods declared on the `Tbot` class."""
    cli_main = _cli_main_module()
    assert hasattr(cli_main.Tbot, "listen")


def _cli_main_module():
    """`timberbot.cli.__init__` re-exports `main` as a function, shadowing the
    submodule attribute. Pull the actual module from sys.modules instead."""
    import importlib
    return importlib.import_module("timberbot.cli.main")


def test_global_flags_thread_into_listen(monkeypatch):
    """`tbot --host=X --auth-token=Y listen` reaches the listen function."""
    cli_main = _cli_main_module()

    captured: dict[str, object] = {}

    def fake_listen(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_main, "listen", fake_listen)
    monkeypatch.setattr(
        cli_main, "_CTX",
        cli_main.GlobalFlags(host="10.0.0.5", auth_token="tok"),
    )

    # Returns None on success; non-zero rcs propagate via SystemExit.
    assert cli_main.Tbot().listen() is None
    assert captured["host"] == "10.0.0.5"
    assert captured["auth_token"] == "tok"


def test_subcommand_local_flag_wins_over_global(monkeypatch):
    """An explicit `tbot listen --host=local` overrides a globally-set host."""
    cli_main = _cli_main_module()

    captured: dict[str, object] = {}

    def fake_listen(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_main, "listen", fake_listen)
    monkeypatch.setattr(
        cli_main, "_CTX", cli_main.GlobalFlags(host="10.0.0.5"),
    )

    cli_main.Tbot().listen(host="local")
    assert captured["host"] == "local"


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def test_format_pretty_handles_missing_fields():
    line = listen_cmd._format_pretty({"event": "x"})
    assert "x" in line
    assert "day ?" in line


def test_is_event_frame_strict():
    assert listen_cmd._is_event_frame({"type": "event"})
    assert not listen_cmd._is_event_frame({"type": "heartbeat"})
    assert not listen_cmd._is_event_frame("event")
    assert not listen_cmd._is_event_frame(None)
