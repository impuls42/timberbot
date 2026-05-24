from __future__ import annotations

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


def serve(
    backend: str | None = None,
    model: str | None = None,
    acp_binary: str | None = None,
    telegram_token: str | None = None,
    mcp_port: int | None = None,
    mcp_host: str | None = None,
    ws_port: int | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    auth_token: str | None = None,
) -> int:
    """Run the MCP game server + ACP agent connector + Telegram UI.

    Args:
        backend: ACP runtime backend (claude or opencode; default: claude).
        model: Model identifier passed to the backend.
        acp_binary: Path or name of the agent CLI to spawn (default: matches backend).
        telegram_token: Telegram bot token (also: TBOT_TELEGRAM_TOKEN env).
        mcp_port: Port for the game MCP HTTP/SSE server (default: 8091).
        mcp_host: Bind address for the game MCP server (default: 127.0.0.1).
        ws_port: WebSocket port on the mod (default: 8086).

    `host`/`port`/`auth_token` are forwarded from the global `tbot --host=` /
    `--port=` / `--auth-token=` flags by `Tbot.serve`; they aren't part of the
    public per-command CLI surface.
    """
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
    host, port = resolve_endpoint(host, port)
    auth_token = resolve_auth_token(auth_token)
    ws_port_resolved = resolve_ws_port(ws_port)
    token = resolve_telegram_token(telegram_token)

    if acp_binary is not None and acp_binary == "":
        print("error: --acp-binary must not be empty", file=sys.stderr)
        return 1

    backend = backend or cfg_data.get("backend", "claude")
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
        ws_port=ws_port_resolved,
        auth_token=auth_token,
        mcp_host=mcp_host or cfg_data.get("mcp_host", "127.0.0.1"),
        mcp_port=mcp_port or int(cfg_data.get("mcp_port", 8091)),
        backend=backend,
        model=model or cfg_data.get("model", "claude-opus-4-7"),
        acp_binary=acp_binary or cfg_data.get("acp_binary", backend),
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

    When an ExceptionGroup carries multiple sibling failures (e.g. several
    tasks in a TaskGroup all blew up at once), we only follow `inner[0]`
    — the resulting one-line CLI summary will be the first task's leaf
    cause, not a digest of all of them. That's a deliberate choice for
    the default WARNING-level output: users running `-vv` still get the
    full ExceptionGroup printed via `log.exception` so no signal is lost.
    """
    inner = getattr(exc, "exceptions", None)
    if isinstance(inner, (list, tuple)) and inner:
        return _root_cause(inner[0])
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _root_cause(cause)
    return exc


