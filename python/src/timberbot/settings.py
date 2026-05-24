"""Resolution of the client endpoint and bearer token.

Resolution order for `(host, port)` — first-match wins:

1. Explicit `host=`/`port=` argument to `resolve_endpoint` (e.g. CLI `--host=`).
2. `TBOT_HOST` / `TBOT_PORT` env vars.
3. `[client]` section in `~/.config/timberbot/config.toml`.
4. Built-in defaults: `127.0.0.1` / `8085`.

Resolution order for `auth_token` — first non-empty wins:

1. Explicit `auth_token=` argument to `resolve_auth_token`.
2. `TBOT_AUTH_TOKEN` env var.
3. `[client].auth_token` in `~/.config/timberbot/config.toml`.
4. None (no auth header sent; the mod only enforces when `authToken` is set
   in its server-side `settings.json`).

The client does not read the mod's `settings.json` — that file is owned by
the C# server and the values relevant to a client (host/port/auth) belong in
the user `config.toml` or environment instead.
"""
from __future__ import annotations

import os
import warnings
from typing import Any

from timberbot.user_config import client_config


def _env_port() -> int | None:
    """Parse `TBOT_PORT` as int, or None if unset/malformed."""
    raw = os.environ.get("TBOT_PORT")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        warnings.warn(
            f"TBOT_PORT='{raw}' is not an integer; ignoring", UserWarning, stacklevel=3,
        )
        return None


def resolve_endpoint(
    host: str | None = None,
    port: int | None = None,
    user_config: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Resolve `(host, port)` per the precedence chain documented at module top.

    `host`/`port` are the explicit overrides (typically CLI flags). When both
    are non-None, the rest of the chain is skipped entirely for that field.

    `user_config` exists so tests can inject in-memory dicts without touching
    the filesystem. Production callers leave it None and let the loader read
    the canonical location.
    """
    uc = user_config if user_config is not None else client_config()

    resolved_host: str
    if host is not None:
        resolved_host = host
    elif "TBOT_HOST" in os.environ and os.environ["TBOT_HOST"]:
        resolved_host = os.environ["TBOT_HOST"]
    elif "host" in uc and isinstance(uc["host"], str):
        resolved_host = uc["host"]
    else:
        resolved_host = "127.0.0.1"

    resolved_port: int
    if port is not None:
        resolved_port = port
    else:
        env_port = _env_port()
        if env_port is not None:
            resolved_port = env_port
        elif "port" in uc and isinstance(uc["port"], int):
            resolved_port = uc["port"]
        else:
            resolved_port = 8085

    return resolved_host, resolved_port


def endpoint_source(
    host: str | None = None,
    port: int | None = None,
    user_config: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return `(host_source, port_source)` labels for the resolved endpoint.

    Mirrors `resolve_endpoint` precedence but reports *where* each field
    came from: `"cli"`, `"env"`, `"config"`, or `"default"`. Useful for
    verbose logging so users can see why the client is hitting the host
    they're hitting.
    """
    uc = user_config if user_config is not None else client_config()
    host_src = (
        "cli" if host is not None
        else "env" if os.environ.get("TBOT_HOST")
        else "config" if isinstance(uc.get("host"), str)
        else "default"
    )
    port_src = (
        "cli" if port is not None
        else "env" if os.environ.get("TBOT_PORT")
        else "config" if isinstance(uc.get("port"), int)
        else "default"
    )
    return host_src, port_src


def auth_token_source(
    auth_token: str | None = None,
    user_config: dict[str, Any] | None = None,
) -> str:
    """Return the source label for the resolved auth token, or `"none"`.

    Mirrors `resolve_auth_token` precedence.
    """
    if auth_token is not None and auth_token.strip():
        return "cli"
    if os.environ.get("TBOT_AUTH_TOKEN", "").strip():
        return "env"
    uc = user_config if user_config is not None else client_config()
    cfg_token = uc.get("auth_token")
    if isinstance(cfg_token, str) and cfg_token.strip():
        return "config"
    return "none"


def source_summary(
    host: str | None = None,
    port: int | None = None,
    auth_token: str | None = None,
    user_config: dict[str, Any] | None = None,
) -> str:
    """One-line "where did each setting come from" summary for verbose logs.

    Example: `host=cli port=default auth=env` — useful when users wonder
    why the client is hitting the wrong server or sending no auth.
    """
    uc = user_config if user_config is not None else client_config()
    h_src, p_src = endpoint_source(host, port, uc)
    a_src = auth_token_source(auth_token, uc)
    return f"host={h_src} port={p_src} auth={a_src}"


def resolve_auth_token(
    auth_token: str | None = None,
    user_config: dict[str, Any] | None = None,
) -> str | None:
    """Resolve the bearer token per the precedence chain documented at module top.

    Returns `None` when no token is configured (the client then omits the
    `Authorization` header and the mod responds 401 only when it has a
    non-empty `authToken` itself). Whitespace-only values at any level are
    treated as unset so the chain falls through cleanly.

    `user_config` exists so tests can inject in-memory dicts without
    touching the filesystem. Production callers leave it None and let the
    loader read the canonical location.
    """
    if auth_token is not None and auth_token.strip():
        return auth_token.strip()

    env_token = os.environ.get("TBOT_AUTH_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    uc = user_config if user_config is not None else client_config()
    cfg_token = uc.get("auth_token")
    if isinstance(cfg_token, str) and cfg_token.strip():
        return cfg_token.strip()

    return None
