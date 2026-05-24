"""`tbot agent {run|list_backends|prompts}` — Python-side agent dispatch.

Exposed to Fire as a class so that `tbot agent run --goal=...` works as a
sub-group. The `run` method replaces the legacy `tbot start` command.
"""
from __future__ import annotations

import sys

from timberbot.agent.prompts import list_packaged_prompts
from timberbot.agent.runner import run_agent
from timberbot.api.client import TimberbotClient
from timberbot.config import config_dir


class AgentCommands:
    """Agent subcommands: run, list_backends, prompts."""

    def run(
        self,
        goal: str,
        backend: str,
        model: str | None = None,
        effort: str | None = None,
        binary: str | None = None,
        command: str | None = None,
        terminal_prefix: str | None = None,
        attach_url: str | None = None,
        prompt: str = "timberbot",
        *,
        host: str | None = None,
        port: int | None = None,
        auth_token: str | None = None,
    ) -> int:
        """Run an AI agent against the live game.

        Args:
            goal: Agent goal / initial prompt.
            backend: Backend name (claude, codex, opencode, custom).
            model: Model identifier passed to the backend.
            effort: Reasoning effort passed to the backend.
            binary: Override the backend's CLI binary path.
            command: Required for --backend=custom: argv template with
                {skill}/{instructions_file}/{prompt}/{prompt_file}/{model}/{effort} placeholders.
            terminal_prefix: Optional command prefix used to wrap the agent
                invocation. Supports {cwd}. Example: "wt -d {cwd} --".
            attach_url: Optional URL of a long-running backend server to attach to
                (currently only the opencode backend supports this, via
                `opencode run --attach <url>`). Overrides config.toml
                [backends.<name>].attach_url. Pass "" to clear a config default.
            prompt: Name of the system prompt to load (default: timberbot).

        Returns the agent process exit code (kept as the return value so unit
        tests can assert it; the Fire dispatcher discards it via main.py).

        `host`/`port`/`auth_token` are forwarded from the global `tbot --host=` /
        `--port=` / `--auth-token=` flags by `_AgentGroup.run`; they aren't part
        of the public per-command CLI surface. Without them, `run_agent` would
        build a default `TimberbotClient(json_mode=True)` pointing at
        `127.0.0.1:8085` with no auth — silently dropping the global flags.
        """
        client: TimberbotClient | None = None
        if host is not None or port is not None or auth_token is not None:
            client = TimberbotClient(
                host=host, port=port, auth_token=auth_token, json_mode=True,
            )
        try:
            return run_agent(
                backend=backend,
                goal=goal,
                model=model,
                effort=effort,
                binary=binary,
                command_template=command,
                terminal_prefix=terminal_prefix,
                attach_url=attach_url,
                prompt_name=prompt,
                client=client,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    def list_backends(self) -> None:
        """List registered agent backends."""
        import timberbot.agent.backends  # noqa: F401  (triggers @register_backend)
        from timberbot.agent.backend import known_backend_names

        for name in known_backend_names():
            print(name)

    def prompts(self) -> None:
        """List packaged + user-override agent prompts."""
        cd = config_dir()
        print("# packaged (read-only, ship with `tbot`)")
        for name in list_packaged_prompts():
            print(f"  {name}")
        print(f"# user overrides (in {cd / 'agent_prompts'})")
        user_dir = cd / "agent_prompts"
        if user_dir.is_dir():
            for f in sorted(user_dir.glob("*.md")):
                print(f"  {f.stem}")
        else:
            print("  (none - run `tbot init` to materialize)")
