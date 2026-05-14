"""User-config directory resolution.

Platform conventions:

  Linux:   $XDG_CONFIG_HOME/timberbot, falling back to ~/.config/timberbot
  macOS:   ~/Library/Application Support/timberbot
  Windows: %APPDATA%\\timberbot, falling back to ~/AppData/Roaming/timberbot

Override with the `TBOT_CONFIG_DIR` env var for testing or unusual setups.
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
