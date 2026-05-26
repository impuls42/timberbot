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
    no_wait: bool = False,
    *,
    host: str | None = None,
    port: int | None = None,
    auth_token: str | None = None,
) -> int:
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
            game. Pass `--no-wait` for the legacy fail-fast behaviour (useful
            in scripts/CI).

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

    # The claude backend now spawns Zed's standalone ACP bridge
    # (`claude-agent-acp`); `claude` itself no longer has an `--acp` mode.
    # opencode still exposes `opencode acp`, so its binary matches the backend.
    default_binary = "claude-agent-acp" if backend == "claude" else backend

    # Single-dialog binding. The bot is pinned to exactly one Telegram
    # chat for the lifetime of the serve instance — preemptive
    # messaging (subagent completions, future game alerts) targets it
    # directly. The earlier `allowed_users` / `allowed_dialogs` list
    # keys were a wider design that's been narrowed; reject them with
    # a clear migration message so an operator upgrading from a
    # previous tbot doesn't accidentally fall through to the default
    # empty config and stand up a bot that can't reach them.
    for legacy in ("allowed_users", "allowed_dialogs"):
        if legacy in tg_data:
            print(
                f"error: [serve.telegram] {legacy} has been removed. "
                "Use `dialog_id = \"<chat-id>\"` instead — a single "
                "Telegram chat id (as a string) the bot binds to at "
                "startup. To find your chat id, message the bot once "
                "and read the chat_id from the logs.",
                file=sys.stderr,
            )
            return 1
    raw_dialog = tg_data.get("dialog_id")
    if raw_dialog is None or raw_dialog == "":
        print(
            "error: [serve.telegram] dialog_id is required. Set "
            "`dialog_id = \"<chat-id>\"` in config.toml — a single "
            "Telegram chat id as a string. The bot binds to this "
            "chat at startup; it needs to know where to send "
            "unsolicited messages.",
            file=sys.stderr,
        )
        return 1
    # Accept int or str in the TOML; normalize to str. (TOML strings
    # are required for supergroup ids that overflow JSON's int range,
    # e.g. -1001234567890.)
    telegram_dialog_id = (
        str(raw_dialog) if isinstance(raw_dialog, int) else str(raw_dialog).strip()
    )
    # Sanity check: parses as int.
    try:
        int(telegram_dialog_id)
    except (TypeError, ValueError):
        print(
            f"error: [serve.telegram] dialog_id must be parseable as a "
            f"Telegram chat id (integer or numeric string); "
            f"got {telegram_dialog_id!r}.",
            file=sys.stderr,
        )
        return 1

    cfg = ServeConfig(
        host=host,
        port=port,
        ws_port=ws_port_resolved,
        auth_token=auth_token,
        mcp_host=mcp_host or cfg_data.get("mcp_host", "127.0.0.1"),
        mcp_port=mcp_port or int(cfg_data.get("mcp_port", 8091)),
        backend=backend,
        model=model or cfg_data.get("model", "claude-opus-4-7"),
        acp_binary=acp_binary or cfg_data.get("acp_binary", default_binary),
        telegram_token=token,
        telegram_dialog_id=telegram_dialog_id,
        allowed_tools=cfg_data.get("allowed_tools", ["game.*"]),
        wait_for_mod=not no_wait,
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


