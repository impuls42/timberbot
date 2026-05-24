"""Integration tests for the Fire-based CLI dispatcher.

Uses pytest-httpserver to simulate the mod's HTTP API so we can verify the
end-to-end flow (argv → global-flag parse → Fire reflection → method dispatch
→ output formatting) without a running game.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pytest_httpserver")


from timberbot.cli import main as cli_main  # noqa: E402


def _run(monkeypatch, argv: list[str], host: str, port: int) -> int:
    """Invoke `timberbot.cli.main` against a stub server."""
    full = [f"--host={host}", f"--port={port}", "--json", *argv]
    monkeypatch.setattr("sys.argv", ["tbot", *full])
    return cli_main([f"--host={host}", f"--port={port}", "--json", *argv])


def test_summary_dispatch(monkeypatch, capsys, httpserver):
    stub = {
        "settlement": "Test", "faction": "Folktails", "science": 7,
        "districts": [], "time": {}, "weather": {},
    }
    httpserver.expect_request("/api/summary").respond_with_json(stub)
    rc = _run(monkeypatch, ["summary"], httpserver.host, httpserver.port)
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["settlement"] == "Test"
    assert parsed["science"] == 7


def test_help_lists_commands(monkeypatch, capsys, httpserver):
    rc = _run(monkeypatch, ["--help"], httpserver.host, httpserver.port)
    out = capsys.readouterr().out
    assert rc == 0
    # The new index has its own header text; the methods + builtins must be there.
    assert "Built-in subcommands:" in out
    assert "Client methods" in out
    for method in ("summary", "buildings", "place_building", "set_speed", "ping"):
        assert method in out
    for sub in ("top", "manager", "launch", "serve", "watch", "listen", "agent", "init"):
        assert sub in out


def test_method_help_renders_fire_screen(monkeypatch, capsys, httpserver):
    """`tbot <method> --help` should show Fire's per-method help (signature + flags).

    Fire writes help to stderr (FireExit(0) after).
    """
    rc = _run(monkeypatch, ["buildings", "--help"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 0
    assert "tbot buildings" in err
    assert "--detail" in err
    assert "--name" in err


def test_builtin_help_renders_fire_screen(monkeypatch, capsys, httpserver):
    """`tbot serve --help` (the original bug — was 'unknown method' before Fire)."""
    rc = _run(monkeypatch, ["serve", "--help"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 0
    assert "tbot serve" in err
    assert "--backend" in err


def test_agent_subgroup_help(monkeypatch, capsys, httpserver):
    """`tbot agent --help` should list the run/list_backends/prompts sub-commands."""
    rc = _run(monkeypatch, ["agent", "--help"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 0
    assert "run" in err
    assert "list_backends" in err
    assert "prompts" in err


def test_error_response_is_propagated(monkeypatch, capsys, httpserver):
    httpserver.expect_request("/api/buildings").respond_with_json({"error": "not_found: id 99"})
    rc = _run(monkeypatch, ["buildings", "--id=99"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 1
    parsed = json.loads(err)
    assert parsed == {"error": "not_found: id 99"}


def test_unknown_method_exits_nonzero(monkeypatch, capsys, httpserver):
    rc = _run(monkeypatch, ["nonexistent_method"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    # Fire prints "ERROR: Could not consume arg: nonexistent_method" and exits 2.
    assert rc != 0
    assert "nonexistent_method" in err


def test_map_command_prints_rendered_string(monkeypatch, capsys, httpserver):
    httpserver.expect_request("/api/tiles").respond_with_json({
        "tiles": [
            {"x": 0, "y": 0, "terrain": 5, "water": 1, "occupants": []},
            {"x": 1, "y": 0, "terrain": 5, "water": 0, "occupants": []},
        ],
    })
    rc = _run(
        monkeypatch,
        ["map", "--x1=0", "--y1=0", "--x2=1", "--y2=0"],
        httpserver.host, httpserver.port,
    )
    out = capsys.readouterr().out
    assert rc == 0
    # The CLI should print the rendered map directly, not a {"rendered": True} marker dict.
    assert "rendered" not in out
    assert "~" in out  # water glyph from render_map


def test_verbose_logs_dispatch_and_request(monkeypatch, capsys, httpserver):
    """`-v` should surface the resolved endpoint and the HTTP round-trip."""
    stub = {
        "settlement": "X", "faction": "Folktails", "science": 0,
        "districts": [], "time": {}, "weather": {},
    }
    httpserver.expect_request("/api/summary").respond_with_json(stub)
    full = [
        f"--host={httpserver.host}", f"--port={httpserver.port}",
        "--json", "-v", "summary",
    ]
    monkeypatch.setattr("sys.argv", ["tbot", *full])
    rc = cli_main(full)
    captured = capsys.readouterr()
    assert rc == 0
    # The two signals we expose at -v: where we're dispatching to + the HTTP call.
    assert "dispatch -> http://" in captured.err
    assert "GET /api/summary" in captured.err
    assert "200" in captured.err


def test_connection_failure_prints_helpful_error(monkeypatch, capsys):
    """Unreachable host -> rc 2 + actionable stderr message, not a traceback."""
    full = ["--host=127.0.0.1", "--port=1", "--json", "summary"]
    monkeypatch.setattr("sys.argv", ["tbot", *full])
    rc = cli_main(full)
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot reach mod" in err
    assert "127.0.0.1:1" in err
    # Should not surface a raw Python traceback (no "Traceback" line).
    assert "Traceback" not in err
