"""`tbot init` — materialize packaged agent prompts into the user config dir.

Idempotent: re-running without `--force` leaves user-edited files alone and
just adds any missing ones. `--force` overwrites all of them.
"""
from __future__ import annotations

import argparse

from tbot.agent.prompts import list_packaged_prompts, load_prompt
from tbot.config import config_dir


def run(args: list[str]) -> int:
    p = argparse.ArgumentParser(prog="tbot init")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing user copies instead of skipping them.")
    p.add_argument("--list", action="store_true",
                   help="Only show what would happen; write nothing.")
    ns = p.parse_args(args)

    target = config_dir() / "agent_prompts"
    target.mkdir(parents=True, exist_ok=True)

    for name in list_packaged_prompts():
        dest = target / f"{name}.md"
        if dest.exists() and not ns.force:
            print(f"skip  {dest} (already exists; use --force to overwrite)")
            continue
        if ns.list:
            print(f"would write  {dest}")
            continue
        content = load_prompt(name)  # packaged source
        dest.write_text(content, encoding="utf-8")
        print(f"wrote {dest}")
    return 0
