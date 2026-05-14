"""Filesystem path resolution for the Timberbot mod folder.

Cross-platform Proton/Wine awareness lands in PR 4. Today this module assumes
the mod folder is under the current user's `~/Documents/Timberborn`, matching
the C# `TimberbotPaths.cs` behavior on Windows/macOS/native Linux.
"""
from __future__ import annotations

import re
from pathlib import Path


def documents_dir() -> Path:
    """Timberborn's per-user data root (`~/Documents/Timberborn`)."""
    return Path.home() / "Documents" / "Timberborn"


def mod_dir() -> Path:
    """The Timberbot mod folder under documents_dir()."""
    return documents_dir() / "Mods" / "Timberbot"


def settings_path() -> Path:
    """The mod's settings.json file."""
    return mod_dir() / "settings.json"


def saves_dir() -> Path:
    """Per-settlement save directory (`~/Documents/Timberborn/Saves`)."""
    return documents_dir() / "Saves"


def memory_base() -> Path:
    """Root of per-settlement brain.toon storage."""
    return mod_dir() / "memory"


_FS_BAD = re.compile(r'[<>:"/\\|?*]')


def sanitize_name(name: str) -> str:
    """Make a settlement name filesystem-safe; never returns empty."""
    return _FS_BAD.sub("_", name).strip() or "unknown"
