"""Top-level orchestrator: gather state, merge prompt, dispatch to a backend."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

from timberbot.agent.backend import (
    AgentBackend,
    AgentContext,
    get_backend,
    known_backend_names,
)
from timberbot.agent.prompts import build_merged_instructions, load_prompt
from timberbot.api.client import TimberbotClient
from timberbot.config import config_dir
from timberbot.user_config import backend_defaults


def _default_log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _ensure_backends_imported() -> None:
    """Import the backends package so its `@register_backend` decorators run."""
    import timberbot.agent.backends  # noqa: F401


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


def _resolve_backend_defaults(
    backend: str,
    *,
    model: str | None,
    effort: str | None,
    command_template: str | None,
    binary: str | None,
    terminal_prefix: str | None,
    attach_url: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Merge `[backends.<name>]` from config.toml over explicit CLI args.

    Precedence for value-picking fields (model/effort/command/binary/
    terminal_prefix): explicit (caller passed not-None) > config.toml value >
    None. Passing `""` for these is taken literally; we don't second-guess.

    Precedence for `attach_url`: same chain, but `""` on either side is
    treated as "explicitly cleared" so a user can disable a config.toml
    default for a single run via `--attach-url ""`. This asymmetry is
    deliberate — attach_url is a toggle for a long-running side process that
    users naturally want to disable occasionally without editing config,
    whereas the other fields are value-picking and silently rewriting `""`
    to "fall through" would surprise anyone who actually wanted the empty
    string.

    Returns the resolved tuple in the same order the caller will pass to
    `AgentContext`.
    """
    defaults = backend_defaults(backend)

    def _cli_wins_with_empty_clears(cli_value: str | None, key: str) -> str | None:
        if cli_value is None or cli_value == "":
            cfg_value = defaults.get(key)
            return cfg_value if cfg_value else None
        return cli_value

    return (
        model if model is not None else defaults.get("model"),
        effort if effort is not None else defaults.get("effort"),
        command_template if command_template is not None else defaults.get("command"),
        binary if binary is not None else defaults.get("binary"),
        terminal_prefix if terminal_prefix is not None else defaults.get("terminal_prefix"),
        _cli_wins_with_empty_clears(attach_url, "attach_url"),
    )


def run_agent(
    *,
    backend: str,
    goal: str,
    model: str | None = None,
    effort: str | None = None,
    binary: str | None = None,
    command_template: str | None = None,
    terminal_prefix: str | None = None,
    attach_url: str | None = None,
    prompt_name: str = "timberbot",
    extra_prompt_names: list[str] | None = None,
    client: TimberbotClient | None = None,
    user_config_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
    check_connection: bool = True,
) -> int:
    """End-to-end agent launch. Returns the agent process exit code.

    1. Resolve the backend (merging `[backends.<name>]` defaults from
       `~/.config/timberbot/config.toml` under any explicit args).
    2. Fetch live colony state via `TimberbotClient.brain(goal)`.
    3. Load the prompt (user config dir wins over packaged), optionally
       prepending any `extra_prompt_names` (e.g. mode-aware fragments from
       `tbot watch`).
    4. Write merged `agent-instructions.md` to the config dir.
    5. Dispatch to backend.run().

    `check_connection`: when True (the default, used by `tbot agent run`),
    issue a one-shot `client.ping()` and return code 2 if the mod is not
    reachable. The `tbot watch` connector owns its own reconnect loop and
    sets this False since it has already verified the connection.
    """
    if log is None:
        log = _default_log

    cd = user_config_dir or config_dir()
    cd.mkdir(parents=True, exist_ok=True)

    (
        model,
        effort,
        command_template,
        binary,
        terminal_prefix,
        attach_url,
    ) = _resolve_backend_defaults(
        backend,
        model=model,
        effort=effort,
        command_template=command_template,
        binary=binary,
        terminal_prefix=terminal_prefix,
        attach_url=attach_url,
    )

    backend_impl = resolve_backend(
        backend,
        command_template=command_template,
        binary_override=binary,
    )

    client = client or TimberbotClient(json_mode=True)
    if check_connection and not client.ping():
        log("error: cannot reach Timberbot HTTP API. is Timberborn running with the mod?")
        return 2

    colony_state = render_colony_state(client, goal=goal)
    prompt_text = load_prompt(prompt_name, config_dir=cd)
    if extra_prompt_names:
        # Prepend each extra fragment ahead of the main prompt. They share the
        # `{config_dir}/agent_prompts/{name}.md` lookup path so user edits win.
        fragments = [load_prompt(n, config_dir=cd) for n in extra_prompt_names]
        prompt_text = "\n\n".join([*[f.rstrip() for f in fragments], prompt_text])
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
        attach_url=attach_url,
    )
    return backend_impl.run(ctx)
