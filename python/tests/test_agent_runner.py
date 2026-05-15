"""Integration tests for the agent runner — stub subprocess.run, verify the flow."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pytest_httpserver")

from timberbot.agent import runner  # noqa: E402
from timberbot.api.client import TimberbotClient  # noqa: E402


def test_render_colony_state_uses_brain(monkeypatch, tmp_path):
    client = TimberbotClient(host="127.0.0.1", port=8085, json_mode=True)

    def fake_brain(goal=None):
        return {"summary": {"day": 5}, "goal": goal or "", "tasks": [], "locations": {}}

    monkeypatch.setattr(client, "brain", fake_brain)
    out = runner.render_colony_state(client, goal="build a dam")
    parsed = json.loads(out)
    assert parsed["goal"] == "build a dam"
    assert parsed["summary"]["day"] == 5


def test_run_agent_pipeline_with_stub_backend(monkeypatch, tmp_path, httpserver):
    """End-to-end: ping/brain succeed, custom backend produces argv, subprocess.run is captured."""
    httpserver.expect_request("/api/ping").respond_with_json({"ready": True})
    httpserver.expect_request("/api/summary").respond_with_json({
        "settlement": "Castle",
        "day": 5,
        "districts": [],
    })

    captured: dict = {}

    def fake_subprocess_run(argv, cwd=None, env=None, check=False):
        captured["argv"] = argv
        captured["cwd"] = cwd

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("timberbot.agent.backend.subprocess.run", fake_subprocess_run)
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))

    client = TimberbotClient(host=httpserver.host, port=httpserver.port, json_mode=True)

    rc = runner.run_agent(
        backend="claude",
        goal="reach 50 beavers",
        model="claude-haiku-4-5",
        client=client,
        user_config_dir=tmp_path,
    )
    assert rc == 0
    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "--system-prompt-file" in argv
    assert "--model" in argv
    assert argv[-1] == "reach 50 beavers"

    # instructions file was written under the config dir
    instructions = Path(captured["cwd"]) / "agent-instructions.md"
    assert instructions.exists()
    content = instructions.read_text(encoding="utf-8")
    assert "## CURRENT COLONY STATE" in content
    assert "Castle" in content  # settlement made it into the colony state JSON


def test_run_agent_returns_2_when_api_unreachable(monkeypatch, tmp_path, capsys):
    """If ping fails, runner exits 2 and never spawns the backend."""

    def never_call(*a, **kw):
        raise AssertionError("subprocess.run should not be called when API is unreachable")

    monkeypatch.setattr("timberbot.agent.backend.subprocess.run", never_call)

    client = TimberbotClient(host="127.0.0.1", port=1, json_mode=True)  # connection refused
    rc = runner.run_agent(
        backend="claude",
        goal="x",
        client=client,
        user_config_dir=tmp_path,
    )
    assert rc == 2


def test_run_agent_unknown_backend_raises_value_error(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        runner.run_agent(
            backend="not-a-real-backend",
            goal="x",
            user_config_dir=tmp_path,
        )
