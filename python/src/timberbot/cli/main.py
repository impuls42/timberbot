"""Entry point for the `tbot` console script.

Layered:

  - `parse_flags` peels global flags from argv (`--json`, `--host=`, `--port=`).
  - `CommandRegistry` resolves `tbot <name> ...` to either a built-in subcommand
    (top/manager/launch/agent) or a generated method-forward command.
  - The catch-all method-forward parses `key:value` args against
    `inspect.signature(TimberbotClient.<name>)` and prints the result as JSON or
    TOON.

Imports are kept lazy where possible so that `from timberbot.cli import main` does
not pull in heavy deps until invoked.
"""
from __future__ import annotations

import contextlib
import io
import json
import platform
import sys
from typing import Any

from pydantic import BaseModel

from timberbot.api.client import TimberbotClient
from timberbot.api.exceptions import TimberbotError
from timberbot.cli.args import (
    GlobalFlags,
    format_usage,
    method_params,
    parse_flags,
    parse_kv_args,
)
from timberbot.cli.dispatcher import (
    Command,
    CommandRegistry,
    doc_first_line,
    public_method_names,
)

# Built-in subcommands route their own argv (they own the rest after the name).
# Anything not in this set falls through to TimberbotClient method dispatch.
_BUILTIN_COMMANDS = {"top", "manager", "launch", "agent", "init", "listen", "watch", "serve"}


def _ensure_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 so emoji/box-drawing chars render on Windows."""
    if sys.stdout.encoding != "utf-8" and isinstance(sys.stdout, io.TextIOWrapper):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")


def _build_registry() -> CommandRegistry:
    """Construct the registry with all built-in subcommands."""
    from timberbot.cli.commands import agent as agent_cmd
    from timberbot.cli.commands import init_cmd
    from timberbot.cli.commands import launch as launch_cmd
    from timberbot.cli.commands import listen as listen_cmd
    from timberbot.cli.commands import manager as manager_cmd
    from timberbot.cli.commands import serve as serve_cmd
    from timberbot.cli.commands import top as top_cmd
    from timberbot.cli.commands import watch as watch_cmd

    registry = CommandRegistry()
    registry.register(Command(
        name="top",
        summary="live colony dashboard",
        handler=top_cmd.run,
        usage="  top [interval:5]",
    ))
    registry.register(Command(
        name="manager",
        summary="auto-manage haulers (keep 1-4 idle)",
        handler=manager_cmd.run,
    ))
    registry.register(Command(
        name="launch",
        summary=(
            "print Steam launch args for the settlement; user opens game manually"
            if platform.system() == "Darwin"
            else "launch Timberborn with --tb-settlement Steam args"
        ),
        handler=launch_cmd.run,
        usage="  launch settlement:<name> [save:<filename>] [timeout:120]",
    ))
    registry.register(Command(
        name="agent",
        summary="launch an AI agent (run / list-backends / prompts)",
        handler=agent_cmd.run,
        usage='  agent run --goal STR [--backend NAME] [--model M] [--effort E] ...',
    ))
    registry.register(Command(
        name="init",
        summary="materialize agent prompts into the user config dir",
        handler=init_cmd.run,
        usage="  init [--force] [--list]",
    ))
    registry.register(Command(
        name="listen",
        summary="subscribe to mod WebSocket and stream events",
        handler=listen_cmd.run,
        usage=("  listen [--pretty] [--forward-to PATH_OR_URL] [--quiet] "
               "[--ws-port N] [--host HOST] [--auth-token T]"),
    ))
    registry.register(Command(
        name="watch",
        summary="long-running WS connector: subscribe to mod, dispatch agent runs",
        handler=watch_cmd.run,
        usage=("  watch [--backend NAME] [--model M] [--effort E] [--prompt NAME] \\\n"
               "        [--ws-port N] [--autonomous-interval SEC] [--once]"),
    ))
    registry.register(Command(
        name="serve",
        summary="run MCP game server + ACP agent connector + Telegram UI",
        handler=serve_cmd.run,
        usage=("  serve [--backend NAME] [--model M] [--telegram-token T]\n"
               "        [--mcp-port N] [--mcp-host HOST] [--ws-port N]"),
    ))
    return registry


def _print_help_index(registry: CommandRegistry) -> None:
    """Print the no-args help screen listing every command."""
    print("usage: tbot [--json] [--host=HOST] [--port=PORT] [--auth-token=TOKEN]")
    print("            <command> key:value ...")
    print()
    print("global flags:")
    print("  --json                output JSON instead of TOON")
    print("  --host=HOST           override target host (env: TBOT_HOST)")
    print("  --port=PORT           override target port (env: TBOT_PORT)")
    print("  --auth-token=TOKEN    bearer token for mod auth (env: TBOT_AUTH_TOKEN)")
    print()
    print("methods:")
    for name in public_method_names(TimberbotClient):
        method = getattr(TimberbotClient, name)
        doc = doc_first_line(method)
        print(f"  {name:30s} {doc}")
        usage = format_usage(name, method)
        if "VALUE" in usage:
            print(f"    {usage.strip()}")
    print()
    for name, cmd in registry.items():
        if name in _BUILTIN_COMMANDS:
            print(f"  {name:30s} {cmd.summary}")


def _print_method_help(method_name: str) -> int:
    if not hasattr(TimberbotClient, method_name):
        print(f"error: unknown method '{method_name}'", file=sys.stderr)
        return 1
    method = getattr(TimberbotClient, method_name)
    if not callable(method):
        print(f"'{method_name}' is a property or not callable.")
        return 0
    doc = method.__doc__ or "No documentation available."
    print(f"Method: {method_name}")
    print("-" * (8 + len(method_name)))
    print(doc.strip())
    print()
    print(f"Usage:\n  {format_usage(method_name, method).strip()}")
    return 0


def _format_output(result: Any, json_mode: bool) -> None:
    if isinstance(result, str):
        print(result)
        return
    if isinstance(result, BaseModel):
        result = result.model_dump()
    if json_mode:
        print(json.dumps(result, indent=2))
        return
    try:
        import toons  # type: ignore[import-not-found]
        print(toons.dumps(result))
    except ImportError:
        print(json.dumps(result, indent=2))


def _format_error(e: TimberbotError, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(e.response, indent=2), file=sys.stderr)
        return
    try:
        import toons  # type: ignore[import-not-found]
        print(toons.dumps(e.response), file=sys.stderr)
    except ImportError:
        print(json.dumps(e.response, indent=2), file=sys.stderr)


def _inject_listen_globals(rest: list[str], flags: GlobalFlags) -> list[str]:
    """Thread the global `--host=` / `--auth-token=` flags into `tbot listen`.

    Only added when the user did *not* already pass the equivalent
    subcommand-local flag, so an explicit `tbot listen --host=local`
    still wins over a globally-set value.
    """
    out = list(rest)
    has_host = any(a == "--host" or a.startswith("--host=") for a in rest)
    has_auth = any(a == "--auth-token" or a.startswith("--auth-token=") for a in rest)
    if flags.host is not None and not has_host:
        out.append(f"--host={flags.host}")
    if flags.auth_token is not None and not has_auth:
        out.append(f"--auth-token={flags.auth_token}")
    return out


def _dispatch_method(
    method_name: str,
    args: list[str],
    *,
    json_mode: bool,
    host: str | None,
    port: int | None,
    auth_token: str | None,
) -> int:
    """Dispatch a method-forward command against TimberbotClient."""
    bot = TimberbotClient(
        host=host, port=port, json_mode=json_mode, auth_token=auth_token,
    )

    if not hasattr(bot, method_name):
        print(f"error: unknown method '{method_name}'", file=sys.stderr)
        return 1
    method = getattr(bot, method_name)
    if not callable(method):
        print(json.dumps(method, indent=2))
        return 0

    params = method_params(method)
    bad: list[str] = []

    def _on_err(msg: str) -> None:
        nonlocal bad
        bad.append(msg)

    kwargs = parse_kv_args(args, params, _on_err)
    if bad:
        for m in bad:
            print(f"error: {m}", file=sys.stderr)
        print(f"usage: {format_usage(method_name, method).strip()}", file=sys.stderr)
        return 1

    try:
        result = method(**kwargs)
    except TimberbotError as e:
        _format_error(e, json_mode)
        return 1

    _format_output(result, json_mode)
    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    raw = list(sys.argv[1:] if argv is None else argv)
    flags = parse_flags(raw)
    registry = _build_registry()

    if not flags.positional:
        _print_help_index(registry)
        return 0 if flags.help_mode else 1

    method_name = flags.positional[0]
    rest = flags.positional[1:]

    if flags.help_mode:
        return _print_method_help(method_name)

    cmd = registry.get(method_name)
    if cmd is not None and method_name in _BUILTIN_COMMANDS:
        # Built-in subcommand owns its own argv handling. For `listen` we
        # re-inject the global value flags so `tbot --host=X --auth-token=Y
        # listen` is honoured the same way it is for method-forward
        # commands. Other built-ins still own their argv unchanged (they
        # have their own conventions and we don't want to break them).
        argv = rest
        if method_name == "listen":
            argv = _inject_listen_globals(rest, flags)
        return cmd.handler(argv)

    return _dispatch_method(
        method_name, rest,
        json_mode=flags.json_mode, host=flags.host, port=flags.port,
        auth_token=flags.auth_token,
    )


if __name__ == "__main__":
    sys.exit(main())
