"""Loader for the mod's `settings.json` and resolution of the client endpoint.

Resolution order for `(host, port)` — first-match wins:

1. Explicit `host=`/`port=` argument to `resolve_endpoint` (e.g. CLI `--host=`).
2. `TBOT_HOST` / `TBOT_PORT` env vars.
3. `[client]` section in `~/.config/timberbot/config.toml`.
4. `httpHost` / `httpPort` in the mod's `settings.json` (legacy compat).
5. Built-in defaults: `127.0.0.1` / `8085`.

The mod's `settings.json` is the canonical place for **server-side** settings
(bind address, security, webhook tuning). Client-only overrides should live in
the user `config.toml` — `settings.py:resolve_endpoint` still reads `httpHost`
from `settings.json` for backwards compatibility, but new setups should prefer
the user config file.

Keys that the mod no longer reads (legacy agent/terminal/python configuration)
are stripped at load time with a one-time `DeprecationWarning` per key, so old
user `settings.json` files don't silently leak their values into the new
config flow.
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

from timberbot.paths import TimberbotPathError, settings_path
from timberbot.user_config import client_config

# Settings keys that PR 4 retires. Older user-managed settings.json files
# still contain them; load_mod_settings() pops them and warns once per key
# per process. Per-backend defaults (model/effort/command) now live in
# ~/.config/timberbot/config.toml; terminal/pythonCommand wrapping moved into
# the `tbot` CLI; allowlist enforcement is hardcoded on the C# side.
DEPRECATED_KEYS: tuple[str, ...] = (
    "terminal",
    "pythonCommand",
    "agentModel",
    "agentEffort",
    "agentCommandTemplate",
    "agentAllowlistEnabled",
    "agentAllowedBinaries",
)

_warned_keys: set[str] = set()


def _strip_deprecated(data: dict[str, Any]) -> dict[str, Any]:
    """Pop deprecated keys from a parsed settings dict, warning once per key.

    Mutates `data` in place AND returns it. Internal helper — the only
    caller (`load_mod_settings`) hands in a fresh dict from `json.load`,
    so in-place mutation is safe. Don't call from public code unless you
    own the dict.
    """
    for key in DEPRECATED_KEYS:
        if key in data:
            data.pop(key, None)
            if key not in _warned_keys:
                _warned_keys.add(key)
                warnings.warn(
                    f"settings.json: '{key}' is deprecated and ignored; "
                    "manage agent settings via ~/.config/timberbot/config.toml",
                    DeprecationWarning,
                    stacklevel=3,
                )
    return data


def load_mod_settings(path: Path | None = None) -> dict[str, Any]:
    """Read `settings.json`. Returns an empty dict if missing or unparseable.

    On machines without a Timberborn install, the resolver can't find a
    Documents directory; we treat that as "no settings" (defaults apply)
    rather than letting `TimberbotPathError` bubble up through every code
    path that builds a `TimberbotClient`.
    """
    if path is None:
        try:
            target = settings_path()
        except TimberbotPathError:
            return {}
    else:
        target = path
    try:
        with open(target) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _strip_deprecated(data)


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
    settings: dict[str, Any] | None = None,
    user_config: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Resolve `(host, port)` per the precedence chain documented at module top.

    `host`/`port` are the explicit overrides (typically CLI flags). When both
    are non-None, the rest of the chain is skipped entirely for that field.

    `settings` and `user_config` exist so tests can inject in-memory dicts
    without touching the filesystem. Production callers leave them None and
    let the loaders read the canonical locations.
    """
    s = settings if settings is not None else load_mod_settings()
    uc = user_config if user_config is not None else client_config()

    resolved_host: str
    if host is not None:
        resolved_host = host
    elif "TBOT_HOST" in os.environ and os.environ["TBOT_HOST"]:
        resolved_host = os.environ["TBOT_HOST"]
    elif "host" in uc and isinstance(uc["host"], str):
        resolved_host = uc["host"]
    elif "httpHost" in s and isinstance(s["httpHost"], str):
        # `httpHost` is the legacy client-side override that used to live in
        # the mod's settings.json. The C# server doesn't write it (server-bind
        # is `listenAddress`, semantically different). Kept for back-compat.
        resolved_host = s["httpHost"]
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
        elif "httpPort" in s:
            try:
                resolved_port = int(s["httpPort"])
            except (TypeError, ValueError):
                resolved_port = 8085
        else:
            resolved_port = 8085

    return resolved_host, resolved_port
