"""Loader for the mod's `settings.json`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tbot.paths import settings_path


def load_mod_settings(path: Path | None = None) -> dict[str, Any]:
    """Read `settings.json`. Returns an empty dict if missing or unparseable."""
    target = path or settings_path()
    try:
        with open(target) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


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
