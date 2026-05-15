"""Integration tests for the CLI dispatcher.

Uses pytest-httpserver to simulate the mod's HTTP API so we can verify the
end-to-end flow (argv → registry → method dispatch → output) without a running
game.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pytest_httpserver")


from tbot.cli import main as cli_main  # noqa: E402


def _run(monkeypatch, argv: list[str], host: str, port: int) -> int:
    """Invoke `tbot.cli.main` against a stub server."""
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
    assert "methods:" in out
    # a few representative methods that must remain in the registry
    for method in ("summary", "buildings", "place_building", "set_speed", "ping"):
        assert method in out
    # built-in subcommands
    for sub in ("top", "manager", "launch", "start"):
        assert sub in out


def test_error_response_is_propagated(monkeypatch, capsys, httpserver):
    httpserver.expect_request("/api/buildings").respond_with_json({"error": "not_found: id 99"})
    rc = _run(monkeypatch, ["buildings", "id:99"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 1
    parsed = json.loads(err)
    assert parsed == {"error": "not_found: id 99"}


def test_unknown_method_exits_nonzero(monkeypatch, capsys, httpserver):
    rc = _run(monkeypatch, ["nonexistent_method"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 1
    assert "unknown method" in err


def test_unknown_param_reports_valid_set(monkeypatch, capsys, httpserver):
    rc = _run(monkeypatch, ["summary", "bogus:1"], httpserver.host, httpserver.port)
    err = capsys.readouterr().err
    assert rc == 1
    assert "unknown parameter" in err


def test_map_command_prints_rendered_string(monkeypatch, capsys, httpserver):
    httpserver.expect_request("/api/tiles").respond_with_json({
        "tiles": [
            {"x": 0, "y": 0, "terrain": 5, "water": 1, "occupants": []},
            {"x": 1, "y": 0, "terrain": 5, "water": 0, "occupants": []},
        ],
    })
    rc = _run(monkeypatch, ["map", "x1:0", "y1:0", "x2:1", "y2:0"], httpserver.host, httpserver.port)
    out = capsys.readouterr().out
    assert rc == 0
    # The CLI should print the rendered map directly, not a {"rendered": True} marker dict.
    assert "rendered" not in out
    assert "~" in out  # water glyph from render_map
