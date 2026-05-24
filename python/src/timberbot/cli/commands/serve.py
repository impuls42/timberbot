from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from timberbot.cli.commands.watch import resolve_ws_port
from timberbot.settings import resolve_auth_token, resolve_endpoint
from timberbot.user_config import serve_config, serve_telegram_config

log = logging.getLogger("timberbot.serve")


def resolve_telegram_token(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("TBOT_TELEGRAM_TOKEN")
    if env:
        return env
    cfg_token = serve_telegram_config().get("token")
    if cfg_token:
        return str(cfg_token)
    print(
        "error: no Telegram token found. Set TBOT_TELEGRAM_TOKEN or add [serve.telegram] token = '...' to config.toml",
        file=sys.stderr,
    )
    sys.exit(1)


def _parse(args: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tbot serve", add_help=True)
    p.add_argument("--backend", default=None, choices=["claude", "opencode"],
                   help="ACP runtime backend (default: claude).")
    p.add_argument("--model", default=None, help="Model identifier passed to the backend.")
    p.add_argument("--acp-binary", dest="acp_binary", default=None,
                   help="Path or name of the agent CLI to spawn (default: matches backend).")
    p.add_argument("--telegram-token", dest="telegram_token", default=None,
                   help="Telegram bot token (also: TBOT_TELEGRAM_TOKEN env).")
    p.add_argument("--mcp-port", dest="mcp_port", type=int, default=None,
                   help="Port for the game MCP HTTP/SSE server (default: 8091).")
    p.add_argument("--mcp-host", dest="mcp_host", default=None,
                   help="Bind address for the game MCP server (default: 127.0.0.1).")
    p.add_argument("--ws-port", dest="ws_port", type=int, default=None,
                   help="WebSocket port on the mod (default: 8086).")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Increase log verbosity (-v INFO, -vv DEBUG).")
    return p.parse_args(args)


def run(args: list[str]) -> int:
    ns = _parse(args)
    _configure_logging(ns.verbose)

    try:
        from timberbot.user_api.serve import ServeConfig, run_serve
    except ImportError:
        print(
            "error: tbot serve requires extra dependencies — run: pip install 'timberbot[serve]'",
            file=sys.stderr,
        )
        return 1

    cfg_data = serve_config()
    tg_data = serve_telegram_config()
    host, port = resolve_endpoint()
    auth_token = resolve_auth_token()
    ws_port = resolve_ws_port(ns.ws_port)
    token = resolve_telegram_token(ns.telegram_token)

    if ns.acp_binary is not None and ns.acp_binary == "":
        print("error: --acp-binary must not be empty", file=sys.stderr)
        return 1

    backend = ns.backend or cfg_data.get("backend", "claude")
    if backend not in ("claude", "opencode"):
        print(f"error: unknown backend {backend!r}; expected 'claude' or 'opencode'", file=sys.stderr)
        return 1

    if "allowed_users" in tg_data:
        raw = tg_data["allowed_users"]
        if not isinstance(raw, list):
            print(
                "error: [serve.telegram] allowed_users must be a list of integers "
                "(Telegram user IDs); got "
                f"{type(raw).__name__}. Refusing to start with a misconfigured allowlist.",
                file=sys.stderr,
            )
            return 1
        try:
            allowed_users = [int(u) for u in raw]
        except (TypeError, ValueError):
            print(
                "error: [serve.telegram] allowed_users must contain integers "
                "(Telegram user IDs). Refusing to start with a misconfigured allowlist.",
                file=sys.stderr,
            )
            return 1
    else:
        allowed_users = []

    cfg = ServeConfig(
        host=host,
        port=port,
        ws_port=ws_port,
        auth_token=auth_token,
        mcp_host=ns.mcp_host or cfg_data.get("mcp_host", "127.0.0.1"),
        mcp_port=ns.mcp_port or int(cfg_data.get("mcp_port", 8091)),
        backend=backend,
        model=ns.model or cfg_data.get("model", "claude-opus-4-7"),
        acp_binary=ns.acp_binary or cfg_data.get("acp_binary", backend),
        telegram_token=token,
        telegram_allowed_users=allowed_users,
        allowed_tools=cfg_data.get("allowed_tools", ["game.*"]),
    )

    try:
        return asyncio.run(run_serve(cfg)) or 0
    except KeyboardInterrupt:
        log.info("serve: interrupted")
        return 0
    except Exception as exc:
        # Friendly handling for the common "user started serve before the
        # mod" path. Anything bubbling out of `run_serve` lands here; we
        # walk the cause chain (and TaskGroup ExceptionGroup branches) so
        # the user sees a clear one-line message instead of a 100-line
        # traceback.
        from timberbot.user_api.serve import ModUnreachableError  # noqa: PLC0415

        # Prefer the most specific known error class anywhere in the chain.
        # ModUnreachableError wraps a ConnectionRefusedError, so a naive
        # "unwrap to leaf" walk would skip past the friendly message.
        known = _find_in_chain(exc, ModUnreachableError)
        if known is not None:
            print(f"error: {known}", file=sys.stderr)
            return 2

        root = _root_cause(exc)
        # Unknown failure — keep the traceback at -vv so we don't lose
        # debug signal, but show a one-line summary by default.
        if log.isEnabledFor(logging.DEBUG):
            log.exception("serve: unexpected failure")
        else:
            print(
                f"error: tbot serve failed: {type(root).__name__}: {root}. "
                "Re-run with -vv for the full traceback.",
                file=sys.stderr,
            )
        return 1


def _walk_chain(exc: BaseException):
    """Yield every exception reachable through ExceptionGroup / __cause__ /
    __context__. Used to find typed errors (e.g. ModUnreachableError) that
    may be buried inside an asyncio.TaskGroup ExceptionGroup wrapping.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        inner = getattr(cur, "exceptions", None)
        if isinstance(inner, (list, tuple)):
            stack.extend(inner)
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                stack.append(nxt)


def _find_in_chain(exc: BaseException, target_cls: type) -> BaseException | None:
    """First exception of type `target_cls` reachable from `exc`, or None."""
    for e in _walk_chain(exc):
        if isinstance(e, target_cls):
            return e
    return None


def _root_cause(exc: BaseException) -> BaseException:
    """Drill through ExceptionGroup / __cause__ / __context__ to the leaf.
    Falls back when no typed friendly error is found in the chain.
    """
    inner = getattr(exc, "exceptions", None)
    if isinstance(inner, (list, tuple)) and inner:
        return _root_cause(inner[0])
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _root_cause(cause)
    return exc


def _configure_logging(verbosity: int) -> None:
    """Back-compat shim; the real impl lives in `cli.logging_setup`."""
    from timberbot.cli.logging_setup import configure_logging
    configure_logging(verbosity)
