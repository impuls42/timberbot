"""Tests for `tbot listen` — the reference webhook receiver."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import requests

pytest.importorskip("aiohttp")
pytest.importorskip("pytest_httpserver")

from aiohttp import web  # noqa: E402

from timberbot.cli.commands import listen as listen_cmd  # noqa: E402

# ---------- in-process server harness -----------------------------------------

def _free_port() -> int:
    """Reserve a free TCP port and immediately release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """Run an aiohttp app in a background thread driving its own event loop.

    We can't use `web.run_app` from a thread because it installs signal
    handlers. `AppRunner` + `TCPSite` is the documented embedding entry point.
    """

    def __init__(self, app: web.Application, port: int) -> None:
        self.app = app
        self.port = port
        self._thread: threading.Thread | None = None
        self._loop = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5), "server failed to start within 5s"
        # Confirm the port actually accepts connections.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server port never became reachable")

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def _serve() -> None:
            runner = web.AppRunner(self.app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", self.port)
            await site.start()
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.05)
            await runner.cleanup()

        try:
            loop.run_until_complete(_serve())
        finally:
            loop.close()


@pytest.fixture
def server_factory() -> Iterator[callable]:
    """Yield a builder that starts a `_Server` and tears it down."""
    instances: list[_Server] = []

    def _build(**app_kwargs) -> tuple[_Server, str]:
        port = _free_port()
        app = listen_cmd.build_app(**app_kwargs)
        srv = _Server(app, port)
        srv.start()
        instances.append(srv)
        return srv, f"http://127.0.0.1:{port}"

    yield _build

    for s in instances:
        s.stop()


# ---------- payload fixtures --------------------------------------------------

SAMPLE_BATCH = [
    {"event": "drought.start", "day": 45, "timestamp": 1711300000, "data": {"duration": 8}},
    {"event": "beaver.died", "day": 45, "timestamp": 1711300000, "data": None},
]


# ---------- tests -------------------------------------------------------------

def test_server_accepts_batched_payload(server_factory, capsys):
    _, base_url = server_factory()
    resp = requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"received": 2}

    captured = capsys.readouterr().out
    assert "drought.start" in captured
    assert "beaver.died" in captured
    # Default mode is raw JSON: each event line round-trips through json.loads.
    lines = [ln for ln in captured.splitlines() if ln.strip().startswith("{")]
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "drought.start"


def test_events_endpoint_alias(server_factory, capsys):
    _, base_url = server_factory()
    resp = requests.post(base_url + "/events", json=SAMPLE_BATCH[:1], timeout=5)
    assert resp.status_code == 200
    out = capsys.readouterr().out
    assert "drought.start" in out


def test_single_event_object_accepted(server_factory, capsys):
    # The receiver is permissive: a single event object also works.
    _, base_url = server_factory()
    resp = requests.post(base_url + "/", json=SAMPLE_BATCH[0], timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"received": 1}
    assert "drought.start" in capsys.readouterr().out


def test_pretty_mode_human_friendly(server_factory, capsys):
    _, base_url = server_factory(pretty=True)
    requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)
    out = capsys.readouterr().out
    # Pretty lines start with "[day N HH:MM:SS] event" — not raw JSON.
    assert "[day 45" in out
    assert "drought.start" in out
    # Raw JSON should NOT appear in pretty mode.
    assert '"event":' not in out and '"event": ' not in out


def test_forward_to_file_writes_jsonl(tmp_path, server_factory, capsys):
    sink = tmp_path / "events.jsonl"
    _, base_url = server_factory(forward_to=f"file://{sink}")
    requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)

    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "drought.start"
    assert json.loads(lines[1])["event"] == "beaver.died"


def test_forward_to_bare_path_also_works(tmp_path, server_factory):
    # No file:// prefix → treat as plain path.
    sink = tmp_path / "nested" / "events.jsonl"
    _, base_url = server_factory(forward_to=str(sink), quiet=True)
    requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)
    assert sink.exists()
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 2


def test_forward_to_http_posts_downstream(server_factory, httpserver, capsys):
    received: list[list[dict]] = []

    def _capture(request):
        received.append(request.get_json())
        return ("", 200, {})

    httpserver.expect_request("/sink", method="POST").respond_with_handler(_capture)
    downstream = httpserver.url_for("/sink")

    _, base_url = server_factory(forward_to=downstream)
    requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)

    # Give aiohttp's ClientSession time to flush (request is awaited in-handler,
    # so it should be done by the time our POST returned, but be defensive).
    deadline = time.time() + 3
    while time.time() < deadline and not received:
        time.sleep(0.05)

    assert received, "downstream never received the batch"
    assert received[0] == SAMPLE_BATCH


def test_quiet_suppresses_stdout(server_factory, capsys, tmp_path):
    sink = tmp_path / "out.jsonl"
    _, base_url = server_factory(quiet=True, forward_to=f"file://{sink}")
    requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)
    out = capsys.readouterr().out
    assert out == ""
    # The file still gets the events.
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 2


def test_invalid_json_returns_400(server_factory):
    _, base_url = server_factory(quiet=True)
    resp = requests.post(
        base_url + "/",
        data="not json",
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    assert resp.status_code == 400


def test_dropped_non_dict_entries_logged_to_stderr(server_factory, capsys):
    _, base_url = server_factory(quiet=True)
    # Two valid events flanking two garbage entries.
    payload = [SAMPLE_BATCH[0], None, "not an object", SAMPLE_BATCH[1]]
    resp = requests.post(base_url + "/", json=payload, timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"received": 2}
    err = capsys.readouterr().err
    assert "dropped 2 non-object" in err


def test_forward_to_file_error_does_not_500(server_factory, capsys, tmp_path):
    # Point --forward-to at a path whose parent already exists as a file —
    # mkdir(parents=True) will raise NotADirectoryError. Receiver should
    # log and still respond 200.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_sink = blocker / "out.jsonl"
    _, base_url = server_factory(quiet=True, forward_to=str(bad_sink))

    resp = requests.post(base_url + "/", json=SAMPLE_BATCH, timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"received": 2}
    assert "forward error" in capsys.readouterr().err


def test_registered_in_main_registry():
    import importlib
    cli_main = importlib.import_module("timberbot.cli.main")
    registry = cli_main._build_registry()
    cmd = registry.get("listen")
    assert cmd is not None
    assert cmd.handler is listen_cmd.run
    assert "listen" in cli_main._BUILTIN_COMMANDS
