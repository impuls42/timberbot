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


def _configure_logging(verbosity: int) -> None:
    """Wire up a minimal stderr logger for `timberbot.*` loggers.

    Applies the verbosity-derived level to the whole `timberbot` package so
    that sub-loggers used by `run_agent` (`timberbot.agent.*`) inherit it too.
    Idempotent — won't double-attach the handler if already configured (e.g.
    by a test harness).
    """
    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    pkg = logging.getLogger("timberbot")
    if not pkg.handlers:
        pkg.addHandler(handler)
    pkg.setLevel(level)
