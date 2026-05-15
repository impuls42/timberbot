"""Unit tests for prompt loading and instruction merging."""
from __future__ import annotations

from timberbot.agent.prompts import (
    build_merged_instructions,
    list_packaged_prompts,
    load_prompt,
)


def test_packaged_prompts_present():
    names = set(list_packaged_prompts())
    # beaver-developer is a repo-local dev-agent (under `agents/` at the
    # repo root), not a shipped prompt — it targets this codebase, not
    # gameplay, so it doesn't get packaged with the `timberbot` wheel.
    # `connector-mode` is the mode-aware preamble prepended by `tbot watch`.
    assert names == {"timberbot", "wirer", "auditor", "scout", "connector-mode"}


def test_load_packaged_prompt_with_and_without_md_suffix():
    a = load_prompt("timberbot")
    b = load_prompt("timberbot.md")
    assert a == b
    assert a.startswith("---")  # frontmatter


def test_user_override_wins_over_packaged(tmp_path):
    prompts_dir = tmp_path / "agent_prompts"
    prompts_dir.mkdir()
    (prompts_dir / "timberbot.md").write_text("CUSTOM USER PROMPT", encoding="utf-8")
    out = load_prompt("timberbot", config_dir=tmp_path)
    assert out == "CUSTOM USER PROMPT"


def test_packaged_fallback_when_user_dir_missing(tmp_path):
    out = load_prompt("timberbot", config_dir=tmp_path / "nonexistent")
    assert out.startswith("---")  # falls back to packaged


def test_build_merged_instructions_writes_expected_layout(tmp_path):
    dest = tmp_path / "out" / "agent-instructions.md"
    path = build_merged_instructions(
        prompt_text="static prompt body\n",
        colony_state='{"day": 5}',
        dest=dest,
    )
    assert path == dest
    content = dest.read_text(encoding="utf-8")
    assert content.startswith("static prompt body")
    assert "## CURRENT COLONY STATE" in content
    assert '"day": 5' in content
