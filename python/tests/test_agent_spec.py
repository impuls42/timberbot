"""Tests for the code-defined agent specification used by `tbot serve`."""
from __future__ import annotations

from timberbot.connector.agent_spec import (
    TIMBERBOT_SPEC,
    AgentSpec,
    render_bootstrap_prompt,
)


def test_timberbot_spec_lists_read_and_write_tools() -> None:
    """Sanity check the canonical tool list — both reads and mutations are present."""
    tools = set(TIMBERBOT_SPEC.allowed_mcp_tools)
    # Read sample
    for t in ("summary", "alerts", "buildings", "prefabs", "weather"):
        assert t in tools, f"read tool {t} missing from spec"
    # Write sample
    for t in ("place_building", "set_recipe", "demolish_building", "pause_building"):
        assert t in tools, f"write tool {t} missing from spec"


def test_timberbot_spec_forbids_host_tools() -> None:
    """The host-side tools that `claude-agent-acp` exposes outside ACP must be in the deny list."""
    forbidden = set(TIMBERBOT_SPEC.forbidden_tools)
    for t in ("Bash", "Terminal", "Read", "Write", "Edit", "WebFetch", "WebSearch"):
        assert t in forbidden, f"{t} must be on the forbid list"


def test_timberbot_spec_tool_lists_do_not_overlap() -> None:
    """A tool can't be both allowed and forbidden — guard against accidental dupes."""
    overlap = set(TIMBERBOT_SPEC.allowed_mcp_tools) & set(TIMBERBOT_SPEC.forbidden_tools)
    assert overlap == set(), f"overlap between allowed and forbidden: {overlap}"


def test_render_bootstrap_includes_identity_and_tool_lists() -> None:
    text = render_bootstrap_prompt(TIMBERBOT_SPEC)
    # Identity prose
    assert "Timberbot" in text
    # Refusal sentence appears verbatim
    assert TIMBERBOT_SPEC.refusal_sentence in text
    # A sample read and write tool both rendered
    assert "summary" in text
    assert "place_building" in text
    # A sample forbidden tool rendered
    assert "Bash" in text
    # Behavior rules are numbered
    assert "1." in text
    # Section markers so the agent (and log readers) can find boundaries
    assert "TIMBERBOT_SYSTEM_BOUNDARY" in text


def test_render_bootstrap_is_deterministic() -> None:
    """Same spec in, same string out — important for log readability and snapshot tests."""
    a = render_bootstrap_prompt(TIMBERBOT_SPEC)
    b = render_bootstrap_prompt(TIMBERBOT_SPEC)
    assert a == b


def test_main_spec_forbids_task_pending_explicit_delegation_tool() -> None:
    """`Task` is in the deny-list until Option B's `mcp__game__delegate`
    MCP tool lands. The runtime's native subagent mechanism is not how
    we delegate."""
    assert "Task" in TIMBERBOT_SPEC.forbidden_tools


def test_subagent_specs_kept_as_data_for_future_delegate_tool() -> None:
    """WIRER/SCOUT/AUDITOR_SPEC stay in code as the canonical definitions
    Option B's `delegate(...)` MCP tool will route through."""
    from timberbot.connector.agent_spec import SUBAGENTS

    assert {s.slug for s in SUBAGENTS} == {"wirer", "scout", "auditor"}
    for s in SUBAGENTS:
        assert s.description, f"{s.slug} missing description"
        assert s.identity, f"{s.slug} missing identity"
        assert s.allowed_mcp_tools, f"{s.slug} missing tools"
        assert s.refusal_sentence, f"{s.slug} missing refusal"


def test_wirer_spec_scoped_to_automation_graph() -> None:
    from timberbot.connector.agent_spec import WIRER_SPEC

    allowed = set(WIRER_SPEC.allowed_mcp_tools)
    assert {"link", "unlink", "configure_automation", "rename_automation"} <= allowed
    # Wirer must NOT be able to place or demolish buildings.
    assert "place_building" not in allowed
    assert "demolish_building" not in allowed
    assert "set_recipe" not in allowed


def test_scout_spec_is_read_only() -> None:
    from timberbot.connector.agent_spec import SCOUT_SPEC

    allowed = set(SCOUT_SPEC.allowed_mcp_tools)
    assert "find_placement" in allowed
    assert "place_building" not in allowed
    assert "place_path" not in allowed


def test_auditor_spec_excludes_every_mutation() -> None:
    from timberbot.connector.agent_spec import AUDITOR_SPEC

    allowed = set(AUDITOR_SPEC.allowed_mcp_tools)
    write_prefixes = ("place_", "set_", "demolish_", "pause_", "unpause_",
                      "mark_", "clear_", "plant_", "migrate", "unlock_",
                      "link", "unlink", "configure_", "rename_", "remove_",
                      "add_", "update_", "find_")
    for name in allowed:
        assert not any(name.startswith(p) for p in write_prefixes), (
            f"auditor must not be allowed mutation tool {name!r}"
        )


def test_custom_spec_renders_independently() -> None:
    spec = AgentSpec(
        slug="test",
        description="A throwaway agent for unit tests.",
        identity="Test agent for verification.",
        allowed_mcp_tools=("foo", "bar"),
        forbidden_tools=("Baz",),
        refusal_sentence="No.",
        behavior_rules=("Do it well.",),
    )
    text = render_bootstrap_prompt(spec)
    assert "Test agent" in text
    assert "foo, bar" in text
    assert "Baz" in text
    assert "1. Do it well." in text
    assert "No." in text


def test_prepare_agent_cwd_creates_empty_sterile_dir(tmp_path, monkeypatch) -> None:
    """The agent cwd is intentionally empty — no CLAUDE.md, no .claude/,
    no .opencode/. All scope flows via the in-prompt bootstrap."""
    from timberbot.user_api.serve import _prepare_agent_cwd

    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path))
    cwd = _prepare_agent_cwd()
    assert str(cwd) == str(tmp_path / "serve")
    # Empty by design — anything else would let the runtime auto-load
    # context we didn't author.
    files = list((tmp_path / "serve").iterdir())
    assert files == [], f"expected sterile cwd, found {files}"
