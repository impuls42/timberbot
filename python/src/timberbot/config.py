"""User-config and user-data directory resolution.

`config_dir()` — where `tbot init` writes prompts and where `config.toml`
lives. Platform conventions:

  Linux:   $XDG_CONFIG_HOME/timberbot, falling back to ~/.config/timberbot
  macOS:   ~/Library/Application Support/timberbot
  Windows: %APPDATA%\\timberbot, falling back to ~/AppData/Roaming/timberbot

Override with `TBOT_CONFIG_DIR` for testing or unusual setups.

`data_dir()` — where per-settlement `brain.toon` files live (post-#43 PR 3).
Mirrors `config_dir()` but targets *data* roots:

  Linux:   $XDG_DATA_HOME/timberbot, falling back to ~/.local/share/timberbot
  macOS:   ~/Library/Application Support/timberbot (no separate data convention)
  Windows: %LOCALAPPDATA%\\timberbot, falling back to ~/AppData/Local/timberbot

Override with `TBOT_DATA_DIR`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("TBOT_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "timberbot"
        return Path.home() / "AppData" / "Roaming" / "timberbot"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "timberbot"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "timberbot"
    return Path.home() / ".config" / "timberbot"


def data_dir() -> Path:
    override = os.environ.get("TBOT_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "timberbot"
        return Path.home() / "AppData" / "Local" / "timberbot"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "timberbot"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "timberbot"
    return Path.home() / ".local" / "share" / "timberbot"
