"""Prompt files shipped as `tbot` package data.

Lookup order:
  1. User config dir (`timberbot.config.config_dir() / "agent_prompts" / <name>.md`)
     so user edits via `tbot init` survive upgrades.
  2. Packaged resource (`timberbot.agent_prompts.<name>.md`).
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path


def _normalize(name: str) -> str:
    return name if name.endswith(".md") else f"{name}.md"


def packaged_prompts_root() -> resources.abc.Traversable:
    return resources.files("timberbot.agent_prompts")


def list_packaged_prompts() -> list[str]:
    """Names (without `.md`) of every prompt shipped with the package."""
    return sorted(
        p.name[:-3]
        for p in packaged_prompts_root().iterdir()
        if p.name.endswith(".md")
    )


def load_prompt(name: str, *, config_dir: Path | None = None) -> str:
    """Read a prompt by name (with or without `.md`).

    If `config_dir` is given and the user has a copy at
    `{config_dir}/agent_prompts/{name}.md`, that wins (so `tbot init` edits
    take precedence). Otherwise falls back to the packaged resource.
    """
    fname = _normalize(name)
    if config_dir is not None:
        user_path = Path(config_dir) / "agent_prompts" / fname
        if user_path.exists():
            return user_path.read_text(encoding="utf-8")
    return (packaged_prompts_root() / fname).read_text(encoding="utf-8")


_COLONY_STATE_HEADER = "## CURRENT COLONY STATE\n"


def build_merged_instructions(
    *,
    prompt_text: str,
    colony_state: str,
    dest: Path,
) -> Path:
    """Write merged system prompt + dynamic colony state to `dest`.

    The output is the static prompt body, a blank line, then a
    `## CURRENT COLONY STATE` section containing the rendered brain block. The
    layout matches the legacy `BuildMergedInstructions` in `TimberbotAgent.cs`
    so existing prompts keep working.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts = [prompt_text.rstrip(), "", _COLONY_STATE_HEADER, colony_state.rstrip(), ""]
    dest.write_text("\n".join(parts), encoding="utf-8")
    return dest
