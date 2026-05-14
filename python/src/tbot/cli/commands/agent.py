"""`tbot agent {run|list-backends|prompts}` — Python-side agent dispatch.

`run` is the new canonical entry point that replaces the legacy `tbot start`
command (which still exists as a deprecated alias forwarding here). All flags
are POSIX-style long-form because they're often consumed from C# / scripts.
"""
from __future__ import annotations

import argparse
import sys

from tbot.agent.prompts import list_packaged_prompts
from tbot.agent.runner import run_agent
from tbot.config import config_dir

_USAGE = (
    "usage: tbot agent {run|list-backends|prompts} [...]\n"
    "  tbot agent run --goal STR --backend NAME [--model M] [--effort E] \\\n"
    "                 [--binary PATH] [--command TEMPLATE] [--terminal-prefix STR] \\\n"
    "                 [--prompt NAME]\n"
    "  tbot agent list-backends\n"
    "  tbot agent prompts\n"
)


def _parse_run(args: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tbot agent run", add_help=True)
    p.add_argument("--goal", required=True, help="Agent goal / initial prompt.")
    # --backend is required and has no default so the C#/Python wire contract is
    # explicit on both sides. Run `tbot agent list-backends` for options.
    p.add_argument("--backend", required=True,
                   help="Backend name. Required; one of: claude, codex, opencode, custom.")
    p.add_argument("--model", default=None, help="Model identifier passed to the backend.")
    p.add_argument("--effort", default=None, help="Reasoning effort passed to the backend.")
    p.add_argument("--binary", default=None,
                   help="Override the backend's CLI binary path (e.g. /opt/claude/claude).")
    p.add_argument("--command", dest="command_template", default=None,
                   help='Required for --backend custom: argv template with {skill}/{instructions_file}/{prompt}/{prompt_file}/{model}/{effort} placeholders.')
    p.add_argument("--terminal-prefix", default=None,
                   help='Optional command prefix used to wrap the agent invocation. Supports {cwd}. Example: "wt -d {cwd} --".')
    p.add_argument("--prompt", dest="prompt_name", default="timberbot",
                   help="Name of the system prompt to load (default: timberbot).")
    return p.parse_args(args)


def _cmd_run(args: list[str]) -> int:
    ns = _parse_run(args)
    try:
        return run_agent(
            backend=ns.backend,
            goal=ns.goal,
            model=ns.model,
            effort=ns.effort,
            binary=ns.binary,
            command_template=ns.command_template,
            terminal_prefix=ns.terminal_prefix,
            prompt_name=ns.prompt_name,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_list_backends(_args: list[str]) -> int:
    # Importing the backends package triggers the `@register_backend` decorators.
    import tbot.agent.backends  # noqa: F401
    from tbot.agent.backend import known_backend_names

    for name in known_backend_names():
        print(name)
    return 0


def _cmd_prompts(_args: list[str]) -> int:
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
    return 0


_SUBCOMMANDS = {
    "run": _cmd_run,
    "list-backends": _cmd_list_backends,
    "prompts": _cmd_prompts,
}


def run(args: list[str]) -> int:
    """Dispatch `tbot agent <subcommand> ...`."""
    if not args:
        print(_USAGE, file=sys.stderr)
        return 1
    sub, rest = args[0], args[1:]
    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        print(f"error: unknown subcommand '{sub}'\n{_USAGE}", file=sys.stderr)
        return 1
    return handler(rest)
