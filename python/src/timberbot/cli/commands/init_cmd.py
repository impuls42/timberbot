"""`tbot init` — materialize packaged agent prompts into the user config dir.

Idempotent: re-running without `--force` leaves user-edited files alone and
just adds any missing ones. `--force` overwrites all of them.
"""
from __future__ import annotations

from timberbot.agent.prompts import list_packaged_prompts, load_prompt
from timberbot.config import config_dir


def init(force: bool = False, list_only: bool = False) -> int:
    """Copy packaged agent prompts into ~/.config/timberbot/agent_prompts/.

    Args:
        force: Overwrite existing user copies instead of skipping them.
        list_only: Only show what would happen; write nothing.
    """
    target = config_dir() / "agent_prompts"
    target.mkdir(parents=True, exist_ok=True)

    for name in list_packaged_prompts():
        dest = target / f"{name}.md"
        if dest.exists() and not force:
            print(f"skip  {dest} (already exists; use --force to overwrite)")
            continue
        if list_only:
            print(f"would write  {dest}")
            continue
        content = load_prompt(name)
        dest.write_text(content, encoding="utf-8")
        print(f"wrote {dest}")
    return 0
