"""Entry point for the `tbot` console script.

Architecture:

  - `parse_global_flags` peels `--host`, `--port`, `--auth-token`, `--json`,
    `-v`/`-vv`/`--verbose`/`--debug`, `--help`/`-h` off raw argv. Whatever
    remains is the command + its arguments.
  - The `Tbot` class is what python-fire reflects into commands. Its
    `__init__` reads the global flags via module-level context (set by
    `main()` before fire is invoked), constructs a `TimberbotClient`, and
    attaches every public client method as a wrapped attribute so Fire sees
    a flat command surface.
  - Forwarded methods are wrapped to format output (TOON vs JSON) and to
    translate `TimberbotError` / connection failures into the same stderr UX
    + exit codes the previous dispatcher had (rc 1/2).
  - Built-in subcommands (top, manager, launch, agent, init, listen, watch,
    serve) are typed methods on `Tbot` that delegate to functions in
    `timberbot.cli.commands.*`.

`--help` UX: pre-process to insert `--` immediately before `--help`/`-h` so
Fire renders its native help for the targeted command. Top-level `tbot --help`
shows our own concise index (Fire's class-level help is too dense for the
~100-method surface).
"""
from __future__ import annotations

import contextlib
import functools
import io
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import fire
import requests
from pydantic import BaseModel

from timberbot.api.client import TimberbotClient
from timberbot.api.exceptions import TimberbotError
from timberbot.cli.commands.agent import AgentCommands
from timberbot.cli.commands.init_cmd import init
from timberbot.cli.commands.launch import launch
from timberbot.cli.commands.listen import listen
from timberbot.cli.commands.manager import manager
from timberbot.cli.commands.serve import serve
from timberbot.cli.commands.top import top
from timberbot.cli.commands.watch import watch
from timberbot.cli.logging_setup import configure_logging
from timberbot.settings import source_summary

log = logging.getLogger("timberbot.cli")


# ---------------------------------------------------------------------------
# Global flags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GlobalFlags:
    """Process-wide flags consumed before fire sees argv."""

    json_mode: bool = False
    help_mode: bool = False
    host: str | None = None
    port: int | None = None
    auth_token: str | None = None
    verbosity: int = 0
    debug: bool = False


_VALUE_PREFIXES = ("--host=", "--port=", "--auth-token=")
_BOOL_GLOBALS = frozenset({
    "--json", "--debug",
    "--verbose", "-v", "-vv", "-vvv",
})
_HELP_TOKENS = frozenset({"--help", "-h"})


def parse_global_flags(argv: list[str]) -> tuple[GlobalFlags, list[str]]:
    """Pull global flags off `argv` and return (flags, remaining_argv).

    `--help`/`-h` is *not* consumed — it stays in `remaining_argv` so the
    `--help` pre-processor in `_promote_help` can route it to Fire's
    per-command help. We just remember that help was requested so `main()`
    can short-circuit to its own index when no command is present.
    """
    host: str | None = None
    port: int | None = None
    auth_token: str | None = None
    json_mode = False
    debug = False
    verbosity = 0
    help_mode = False
    remaining: list[str] = []

    for tok in argv:
        if tok in _HELP_TOKENS:
            help_mode = True
            remaining.append(tok)
            continue
        if tok == "--json":
            json_mode = True
            continue
        if tok == "--debug":
            debug = True
            continue
        if tok in ("--verbose", "-v"):
            verbosity += 1
            continue
        if tok == "-vv":
            verbosity += 2
            continue
        if tok == "-vvv":
            verbosity += 3
            continue
        if tok.startswith("--host="):
            host = tok.split("=", 1)[1]
            continue
        if tok.startswith("--port="):
            raw = tok.split("=", 1)[1]
            try:
                port = int(raw)
            except ValueError:
                # Surface bad input instead of silently falling through to
                # the default port. Defer the exit to `main()` after logging
                # is configured so `-v` still shows the parsed flags first.
                print(
                    f"error: --port={raw!r} is not an integer",
                    file=sys.stderr,
                )
                raise SystemExit(2) from None
            continue
        if tok.startswith("--auth-token="):
            auth_token = tok.split("=", 1)[1]
            continue
        remaining.append(tok)

    return (
        GlobalFlags(
            json_mode=json_mode,
            help_mode=help_mode,
            host=host,
            port=port,
            auth_token=auth_token,
            verbosity=verbosity,
            debug=debug,
        ),
        remaining,
    )


def _promote_help(argv: list[str]) -> list[str]:
    """Insert `--` immediately before any `--help`/`-h` token.

    Fire treats `cmd --help` as "pass --help as the value of cmd's first
    flag"; the canonical idiom for "show help" is `cmd -- --help`. Promoting
    here means users never have to know about the `--` separator.
    """
    if not any(tok in _HELP_TOKENS for tok in argv):
        return list(argv)
    out: list[str] = []
    for tok in argv:
        if tok in _HELP_TOKENS:
            if not out or out[-1] != "--":
                out.append("--")
            out.append("--help")
        else:
            out.append(tok)
    return out


# Process-wide context the `Tbot` constructor reads from. Set by `main()`
# before `fire.Fire` runs. Mutated only once per invocation.
_CTX: GlobalFlags = GlobalFlags()


def _set_context(flags: GlobalFlags) -> None:
    global _CTX
    _CTX = flags


# ---------------------------------------------------------------------------
# Output / error rendering
# ---------------------------------------------------------------------------


def _format_output(result: Any, json_mode: bool) -> None:
    if result is None:
        return
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


# ---------------------------------------------------------------------------
# Client-method wrapping
# ---------------------------------------------------------------------------


def _public_method_names(cls: type) -> list[str]:
    """Names of public callable methods on `cls`, sorted."""
    out: list[str] = []
    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name, None)
        if callable(attr):
            out.append(name)
    return out


def _wrap_forwarded(
    method: Callable[..., Any],
    *,
    json_mode: bool,
    client_url: str,
) -> Callable[..., None]:
    """Wrap a bound `TimberbotClient` method so Fire's reflection still
    sees its signature, the result is formatted as TOON/JSON, and known
    failure modes turn into the same stderr UX the legacy dispatcher had.
    """
    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            result = method(*args, **kwargs)
        except TimberbotError as e:
            _format_error(e, json_mode)
            raise SystemExit(1) from None
        except (requests.ConnectionError, requests.Timeout) as e:
            print(
                f"error: cannot reach mod at {client_url} ({type(e).__name__}). "
                f"Is Timberborn running with the mod loaded? Run with -vv for details.",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        _format_output(result, json_mode)

    return wrapper


def _wrap_builtin(fn: Callable[..., int]) -> Callable[..., None]:
    """Wrap a builtin subcommand so its int return code never lands in Fire's
    output. Non-zero rc → `SystemExit(rc)` (so `tbot` exits with that code);
    zero rc → return None (so Fire prints nothing).
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        rc = fn(*args, **kwargs)
        if rc:
            raise SystemExit(rc)
    return wrapper


class _AgentGroup:
    """Agent subcommands: run, list_backends, prompts."""

    # Internal: wraps `AgentCommands` so int rcs from `run` don't leak into
    # Fire's printed output. `AgentCommands.run` keeps returning `int` (used
    # by unit tests); the wrapper here translates non-zero rcs to `SystemExit`
    # for the CLI and relies on `functools.wraps` so Fire still picks up the
    # typed signature and docstring for `tbot agent run --help`.

    def __init__(self) -> None:
        self._inner = AgentCommands()

    @functools.wraps(AgentCommands.run)
    def run(self, *args: Any, **kwargs: Any) -> None:
        # Thread global --host/--port/--auth-token through to the
        # `TimberbotClient` that `run_agent` uses for state reads. Without
        # this, `tbot --host=X agent run …` would silently fall back to
        # 127.0.0.1:8085. An explicit per-call kwarg still wins.
        ctx = _CTX
        kwargs.setdefault("host", ctx.host)
        kwargs.setdefault("port", ctx.port)
        kwargs.setdefault("auth_token", ctx.auth_token)
        rc = self._inner.run(*args, **kwargs)
        if rc:
            raise SystemExit(rc)

    @functools.wraps(AgentCommands.list_backends)
    def list_backends(self) -> None:
        self._inner.list_backends()

    @functools.wraps(AgentCommands.prompts)
    def prompts(self) -> None:
        self._inner.prompts()


# ---------------------------------------------------------------------------
# The Tbot class (Fire entrypoint)
# ---------------------------------------------------------------------------


class Tbot:
    """tbot — CLI for the Timberbot Timberborn mod.

    Global flags (before the command name):
      --host=HOST         override target host (env: TBOT_HOST)
      --port=PORT         override target port (env: TBOT_PORT)
      --auth-token=TOKEN  bearer token (env: TBOT_AUTH_TOKEN)
      --json              output JSON instead of TOON
      -v / -vv / --debug  log dispatch / HTTP / bodies (TBOT_DEBUG=1 forces -vv)

    Run `tbot <command> --help` for command-specific options.
    """

    def __init__(self) -> None:
        ctx = _CTX
        self._json = ctx.json_mode
        client = TimberbotClient(
            host=ctx.host, port=ctx.port, auth_token=ctx.auth_token,
        )
        self._client = client
        log.info(
            "dispatch -> %s (%s)",
            client.url, source_summary(ctx.host, ctx.port, ctx.auth_token),
        )
        for name in _public_method_names(TimberbotClient):
            method = getattr(client, name)
            setattr(
                self, name,
                _wrap_forwarded(method, json_mode=ctx.json_mode, client_url=client.url),
            )

    # `init` writes prompts to disk and never talks to the mod, so the global
    # connection flags are irrelevant to it. The other builtins are instance
    # methods below so they can read `_CTX` and thread the global `--host=` /
    # `--port=` / `--auth-token=` flags through to the mod-facing client /
    # resolvers. The wrapper methods only expose the *public* per-command
    # signature to Fire's `--help` — connection flags are surfaced once at
    # the top level (`tbot --help`), not duplicated per command.
    init = staticmethod(_wrap_builtin(init))
    agent = _AgentGroup()

    def top(self, interval: int = 5) -> None:
        """Live colony dashboard. Tick every `interval` seconds. Press q to quit, 0-3 to set game speed."""
        ctx = _CTX
        rc = top(
            interval=interval,
            host=ctx.host, port=ctx.port, auth_token=ctx.auth_token,
        )
        if rc:
            raise SystemExit(rc)

    def manager(self) -> None:
        """Auto-pause low-priority buildings to keep idle haulers in band (1-4)."""
        ctx = _CTX
        rc = manager(host=ctx.host, port=ctx.port, auth_token=ctx.auth_token)
        if rc:
            raise SystemExit(rc)

    def launch(self, settlement: str, save: str = "", timeout: int = 120) -> None:
        """Kill any running Timberborn, launch via Steam with --tb-settlement/--tb-save, wait for the API to come up."""
        ctx = _CTX
        rc = launch(
            settlement=settlement, save=save, timeout=timeout,
            host=ctx.host, port=ctx.port, auth_token=ctx.auth_token,
        )
        if rc:
            raise SystemExit(rc)

    def watch(
        self,
        backend: str = "claude",
        model: str | None = None,
        effort: str | None = None,
        prompt: str = "timberbot",
        ws_port: int | None = None,
        autonomous_interval: float = 60.0,
        heartbeat_interval: float = 30.0,
        once: bool = False,
    ) -> None:
        """Long-running connector: subscribe to the mod's WebSocket and dispatch agent runs.

        Args:
            backend: Agent backend to dispatch (default: claude).
            model: Model identifier passed to the backend.
            effort: Reasoning effort passed to the backend.
            prompt: Name of the base system prompt to load (default: timberbot).
            ws_port: WebSocket port on the mod. Resolution chain: this flag → TBOT_WS_PORT env
                → [client].ws_port in config.toml → 8086.
            autonomous_interval: Seconds between autonomous-mode cycles (default: 60).
            heartbeat_interval: Seconds between WS heartbeat frames (default: 30).
            once: Run until a single trigger fires, then exit (useful for debugging).
        """
        ctx = _CTX
        rc = watch(
            backend=backend, model=model, effort=effort, prompt=prompt,
            ws_port=ws_port,
            autonomous_interval=autonomous_interval,
            heartbeat_interval=heartbeat_interval,
            once=once,
            host=ctx.host, port=ctx.port, auth_token=ctx.auth_token,
        )
        if rc:
            raise SystemExit(rc)

    def serve(
        self,
        backend: str | None = None,
        model: str | None = None,
        acp_binary: str | None = None,
        telegram_token: str | None = None,
        mcp_port: int | None = None,
        mcp_host: str | None = None,
        ws_port: int | None = None,
        no_wait: bool = False,
    ) -> None:
        """Run the MCP game server + ACP agent connector + Telegram UI.

        Args:
            backend: ACP runtime backend (claude or opencode; default: claude).
            model: Model identifier passed to the backend.
            acp_binary: Path or name of the agent CLI to spawn (default:
                claude-agent-acp for the claude backend, opencode for opencode).
            telegram_token: Telegram bot token (also: TBOT_TELEGRAM_TOKEN env).
            mcp_port: Port for the game MCP HTTP/SSE server (default: 8091).
            mcp_host: Bind address for the game MCP server (default: 127.0.0.1).
            ws_port: WebSocket port on the mod (default: 8086).
            no_wait: Fail fast if the mod isn't reachable at startup. By default
                `tbot serve` retries the startup ping with exp_backoff (1s→30s)
                until the mod responds, so you can launch the server before the
                game. Pass `--no-wait` for the legacy fail-fast behaviour.
        """
        ctx = _CTX
        rc = serve(
            backend=backend, model=model, acp_binary=acp_binary,
            telegram_token=telegram_token,
            mcp_port=mcp_port, mcp_host=mcp_host, ws_port=ws_port,
            no_wait=no_wait,
            host=ctx.host, port=ctx.port, auth_token=ctx.auth_token,
        )
        if rc:
            raise SystemExit(rc)

    def listen(  # noqa: D401 — Fire reads the docstring verbatim
        self,
        pretty: bool = False,
        forward_to: str | None = None,
        quiet: bool = False,
        ws_port: int | None = None,
        host: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        """Subscribe to the mod's WebSocket and stream event frames.

        Args:
            pretty: Print one human-friendly line per event instead of raw JSON.
            forward_to: Append each event as JSON to a file (file:// or bare path)
                or POST it as a 1-element batch to a URL (http(s)://).
            quiet: Suppress stdout output (only --forward-to receives events).
            ws_port: WebSocket port on the mod (defaults to the resolved HTTP port).
            host: Mod host. Overrides TBOT_HOST and config.toml [client].host.
                Defaults to the global --host=HOST flag if unset.
            auth_token: Bearer token. Defaults to the global --auth-token flag if unset.
        """
        ctx = _CTX
        rc = listen(
            pretty=pretty,
            forward_to=forward_to,
            quiet=quiet,
            ws_port=ws_port,
            host=host if host is not None else ctx.host,
            auth_token=auth_token if auth_token is not None else ctx.auth_token,
        )
        if rc:
            raise SystemExit(rc)


# ---------------------------------------------------------------------------
# Top-level help (custom — Fire's default is too dense for ~100 methods)
# ---------------------------------------------------------------------------


def _print_help_index() -> None:
    """Print the top-level help screen.

    Sections:
      1. Global flags
      2. Built-in subcommands (top/serve/agent/…) with one-line summaries
      3. Forwarded TimberbotClient methods, alphabetical
    """
    print("usage: tbot [global flags] <command> [options]")
    print()
    print("Global flags (before the command):")
    print("  --host=HOST           override target host (env: TBOT_HOST)")
    print("  --port=PORT           override target port (env: TBOT_PORT)")
    print("  --auth-token=TOKEN    bearer token (env: TBOT_AUTH_TOKEN)")
    print("  --json                output JSON instead of TOON")
    print("  -v / -vv / --debug    log dispatch / HTTP / bodies")
    print()
    print("Built-in subcommands:")
    builtins = [
        ("top", "live colony dashboard"),
        ("manager", "auto-manage haulers (keep 1-4 idle)"),
        ("launch", "launch Timberborn with --tb-settlement Steam args"),
        ("agent", "run / list_backends / prompts (use `tbot agent --help`)"),
        ("init", "materialize agent prompts into the user config dir"),
        ("listen", "subscribe to mod WebSocket and stream events"),
        ("watch", "long-running WS connector: subscribe to mod, dispatch agent runs"),
        ("serve", "run MCP game server + ACP agent connector + Telegram UI"),
    ]
    for name, summary in builtins:
        print(f"  {name:14s} {summary}")
    print()
    print("Client methods (run `tbot <name> --help` for arguments):")
    for name in _public_method_names(TimberbotClient):
        method = getattr(TimberbotClient, name)
        doc = (method.__doc__ or "").split("\n")[0].strip()
        print(f"  {name:30s} {doc}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _ensure_utf8_stdout() -> None:
    if sys.stdout.encoding != "utf-8" and isinstance(sys.stdout, io.TextIOWrapper):
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    raw = list(sys.argv[1:] if argv is None else argv)
    flags, remaining = parse_global_flags(raw)
    configure_logging(flags.verbosity, debug=flags.debug)
    _set_context(flags)

    if flags.help_mode and not [t for t in remaining if t not in _HELP_TOKENS]:
        _print_help_index()
        return 0

    if not remaining:
        _print_help_index()
        return 1

    fire_argv = _promote_help(remaining)

    try:
        fire.Fire(Tbot, name="tbot", command=fire_argv)
        return 0
    except SystemExit as e:
        code = e.code
        if isinstance(code, int):
            return code
        return 0 if code is None else 1
    except fire.core.FireExit as e:
        return e.code
    except TimberbotError as e:
        _format_error(e, flags.json_mode)
        return 1
    except (requests.ConnectionError, requests.Timeout) as e:
        print(
            f"error: cannot reach mod ({type(e).__name__}). "
            f"Is Timberborn running with the mod loaded? Run with -vv for details.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
