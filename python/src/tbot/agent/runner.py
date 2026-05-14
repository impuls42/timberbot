"""Top-level orchestrator: gather state, merge prompt, dispatch to a backend."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from tbot.agent.backend import (
    AgentBackend,
    AgentContext,
    get_backend,
    known_backend_names,
)
from tbot.agent.prompts import build_merged_instructions, load_prompt
from tbot.api.client import TimberbotClient
from tbot.config import config_dir


def _default_log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _ensure_backends_imported() -> None:
    """Import the backends package so its `@register_backend` decorators run."""
    import tbot.agent.backends  # noqa: F401


def resolve_backend(name: str, *, command_template: str | None = None,
                    binary_override: str | None = None) -> AgentBackend:
    """Build a backend instance, raising a friendly error on unknown names."""
    _ensure_backends_imported()
    if name not in known_backend_names():
        raise ValueError(
            f"unknown backend '{name}'. known: {', '.join(known_backend_names())}"
        )
    kwargs: dict[str, object] = {}
    if name == "custom":
        if not command_template:
            raise ValueError("backend 'custom' requires --command \"<template>\"")
        kwargs["template"] = command_template
    if binary_override and name != "custom":
        kwargs["binary_override"] = binary_override
    return get_backend(name, **kwargs)


def render_colony_state(client: TimberbotClient, goal: str | None) -> str:
    """Render the `brain` snapshot into a markdown-friendly block.

    Delegates to `TimberbotClient.brain` (which already returns
    `summary` + `goal` + `tasks` + `locations`) and dumps it as JSON. Backends
    that want richer formatting can render their own block; this is the
    no-frills default and matches the legacy `BuildStartupPrompt` shape.
    """
    snapshot = client.brain(goal=goal)
    return json.dumps(snapshot, indent=2)


def run_agent(
    *,
    backend: str,
    goal: str,
    model: str | None = None,
    effort: str | None = None,
    binary: str | None = None,
    command_template: str | None = None,
    terminal_prefix: str | None = None,
    prompt_name: str = "timberbot",
    client: TimberbotClient | None = None,
    user_config_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """End-to-end agent launch. Returns the agent process exit code.

    1. Resolve the backend.
    2. Fetch live colony state via `TimberbotClient.brain(goal)`.
    3. Load the prompt (user config dir wins over packaged).
    4. Write merged `agent-instructions.md` to the config dir.
    5. Dispatch to backend.run().
    """
    if log is None:
        log = _default_log

    cd = user_config_dir or config_dir()
    cd.mkdir(parents=True, exist_ok=True)

    backend_impl = resolve_backend(
        backend,
        command_template=command_template,
        binary_override=binary,
    )

    client = client or TimberbotClient(json_mode=True)
    if not client.ping():
        log("error: cannot reach Timberbot HTTP API. is Timberborn running with the mod?")
        return 2

    colony_state = render_colony_state(client, goal=goal)
    prompt_text = load_prompt(prompt_name, config_dir=cd)
    instructions_file = build_merged_instructions(
        prompt_text=prompt_text,
        colony_state=colony_state,
        dest=cd / "agent-instructions.md",
    )

    log(f"agent: backend={backend} prompt={prompt_name} instructions={instructions_file}")

    ctx = AgentContext(
        goal=goal,
        instructions_file=instructions_file,
        cwd=cd,
        model=model,
        effort=effort,
        binary_override=binary,
        terminal_prefix=terminal_prefix,
    )
    return backend_impl.run(ctx)
