"""User-facing config file at `<config_dir>/config.toml`.

Client-only settings (target host/port, default format) and per-backend
defaults (model, effort, command template) live here so users can set them
once instead of repeating them on every `tbot agent run` invocation. The
mod's own `settings.json` stays scoped to server-side concerns (bind,
security, perf, webhook).

Schema::

    [client]
    host = "127.0.0.1"
    port = 8085
    default_format = "toon"    # or "json"
    auth_token = "shared-secret"  # matches mod's authToken in settings.json

    [backends.claude]
    model = "claude-opus-4-7"
    effort = "high"

    [backends.opencode]
    model = "glm-4.6"
    # Optional: attach to a long-running `opencode serve` instance instead of
    # spawning a fresh process per cycle (matches the Steam Deck / phone-driven
    # workflow). Overridden by `tbot agent run --attach-url <url>`.
    attach_url = "http://127.0.0.1:4096"

Missing file or parse errors are non-fatal: the loader returns an empty
mapping and the rest of the resolution chain fills in defaults. Each load
emits at most one `UserWarning` per process per error condition.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:  # pragma: no cover - exercised on 3.10 CI only
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from timberbot.config import config_dir

_warned_paths: set[str] = set()


def config_path() -> Path:
    """The expected location of the user `config.toml`. The file may not exist."""
    return config_dir() / "config.toml"


def _warn_once(path: Path, reason: str) -> None:
    key = f"{path}:{reason}"
    if key in _warned_paths:
        return
    _warned_paths.add(key)
    warnings.warn(f"{path}: {reason}; falling back to defaults", UserWarning, stacklevel=3)


def load_user_config(path: Path | None = None) -> dict[str, Any]:
    """Parse the user `config.toml` and return its top-level mapping.

    Returns an empty dict if the file is missing, unreadable, or parse-broken.
    The result is the raw TOML mapping — callers reach into `[client]` /
    `[backends.<name>]` via `client_config()` / `backend_defaults()`.
    """
    target = path if path is not None else config_path()
    if not target.is_file():
        return {}
    try:
        with open(target, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _warn_once(target, f"could not load config.toml ({exc.__class__.__name__})")
        return {}
    if not isinstance(data, dict):
        _warn_once(target, "top-level config.toml is not a table")
        return {}
    return data


def client_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the `[client]` section as a plain dict. Empty if absent."""
    data = config if config is not None else load_user_config()
    section = data.get("client")
    return section if isinstance(section, dict) else {}


def backend_defaults(backend: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the `[backends.<backend>]` section as a plain dict. Empty if absent."""
    data = config if config is not None else load_user_config()
    backends = data.get("backends")
    if not isinstance(backends, dict):
        return {}
    section = backends.get(backend)
    return section if isinstance(section, dict) else {}


def serve_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the `[serve]` section as a plain dict. Empty if absent."""
    data = config if config is not None else load_user_config()
    section = data.get("serve")
    return section if isinstance(section, dict) else {}


def serve_telegram_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the `[serve.telegram]` subsection as a plain dict. Empty if absent."""
    serve = serve_config(config)
    section = serve.get("telegram")
    return section if isinstance(section, dict) else {}


def reset_warning_cache() -> None:
    """Test helper: clear the warned-paths cache so re-loads can re-warn."""
    _warned_paths.clear()
