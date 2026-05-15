"""Loader for the mod's `settings.json`.

Keys that the mod no longer reads (legacy agent/terminal/python configuration)
are stripped at load time with a one-time `DeprecationWarning` per key, so old
user `settings.json` files don't silently leak their values into the new
config flow.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from timberbot.paths import settings_path

# Settings keys that PR 4 retires. Older user-managed settings.json files
# still contain them; load_mod_settings() pops them and warns once per key
# per process. Per-backend defaults (model/effort/command) now live in
# ~/.config/timberbot/config.toml; terminal/pythonCommand wrapping moved into
# the `tbot` CLI; allowlist enforcement is hardcoded on the C# side.
DEPRECATED_KEYS: tuple[str, ...] = (
    "terminal",
    "pythonCommand",
    "agentBinary",
    "agentModel",
    "agentEffort",
    "agentCommandTemplate",
    "agentAllowlistEnabled",
    "agentAllowedBinaries",
)

_warned_keys: set[str] = set()


def _strip_deprecated(data: dict[str, Any]) -> dict[str, Any]:
    """Pop deprecated keys from a parsed settings dict, warning once per key."""
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
    """Read `settings.json`. Returns an empty dict if missing or unparseable."""
    target = path or settings_path()
    try:
        with open(target) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return _strip_deprecated(data)


def resolve_endpoint(
    host: str | None = None,
    port: int | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Resolve `(host, port)` from explicit args, then settings.json, then defaults."""
    s = settings if settings is not None else load_mod_settings()
    return (
        host if host is not None else s.get("httpHost", "127.0.0.1"),
        port if port is not None else int(s.get("httpPort", 8085)),
    )
