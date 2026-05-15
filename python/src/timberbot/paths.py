"""Filesystem path resolution for the Timberbot mod folder.

`documents_dir()` is the entry point. Resolution order:

1. The cached value, if `set_documents_dir_override()` has been called or
   `documents_dir()` was previously resolved.
2. `$TBOT_DOCUMENTS_DIR` env var, if set. No existence check — the user takes
   responsibility for a not-yet-created directory.
3. `~/Documents/Timberborn`, if it exists. Matches the C# `TimberbotPaths.cs`
   behavior on Windows, macOS, and native Linux.
4. On Linux, a scan of Wine prefixes for Proton-installed Timberborn at
   `~/.steam/steam/steamapps/compatdata/<appid>/pfx/drive_c/users/steamuser/...
   /Documents/Timberborn` (also `My Documents/Timberborn`). The Timberborn
   Steam AppID `1062090` is preferred but not required, so beta branches still
   match.
5. Otherwise, raise `TimberbotPathError`.

The mod folder is resolved separately by `mod_dir()`:

1. `set_mod_dir_override()` (e.g. CLI `--mod-dir=`).
2. `$TBOT_MOD_DIR` env var.
3. `documents_dir() / "Mods" / "Timberbot"`.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Timberborn's Steam AppID. Used to prefer the correct Proton prefix when
# several compatdata directories contain a Timberborn folder.
_TIMBERBORN_APPID = "1062090"

_documents_dir_override: Path | None = None
_documents_dir_cached: Path | None = None
_mod_dir_override: Path | None = None


class TimberbotPathError(Exception):
    """Raised when Timberborn's Documents directory cannot be located."""


def set_documents_dir_override(path: Path | None) -> None:
    """Pin the value returned by `documents_dir()`. Pass `None` to clear.

    Also clears any cached resolver result so the next call re-resolves.
    """
    global _documents_dir_override, _documents_dir_cached
    _documents_dir_override = Path(path) if path is not None else None
    _documents_dir_cached = None


def set_mod_dir_override(path: Path | None) -> None:
    """Pin the value returned by `mod_dir()`. Pass `None` to clear."""
    global _mod_dir_override
    _mod_dir_override = Path(path) if path is not None else None


def reset_cache() -> None:
    """Clear all overrides and cached resolutions. Test helper."""
    global _documents_dir_cached
    set_documents_dir_override(None)
    set_mod_dir_override(None)
    _documents_dir_cached = None


def _candidate_proton_paths() -> list[Path]:
    """Enumerate Proton/Wine compatdata locations that contain a Timberborn dir.

    Returns the matches sorted with the Timberborn AppID first, so a hand-crafted
    or beta-branch prefix only wins if there is no canonical install.

    Limitation: this scan assumes the standard Proton-managed username
    (`steamuser`). Custom Wine prefixes with a different Windows username
    won't be discovered — those setups should set `TBOT_DOCUMENTS_DIR` or
    pass `--documents-dir` explicitly.
    """
    if sys.platform not in ("linux", "linux2"):
        return []
    compatdata = Path.home() / ".steam" / "steam" / "steamapps" / "compatdata"
    if not compatdata.is_dir():
        return []
    matches: list[Path] = []
    for appdir in compatdata.iterdir():
        if not appdir.is_dir():
            continue
        users = appdir / "pfx" / "drive_c" / "users" / "steamuser"
        for docs in (users / "Documents", users / "My Documents"):
            target = docs / "Timberborn"
            if target.is_dir():
                matches.append(target)
                break
    matches.sort(key=lambda p: (_TIMBERBORN_APPID not in p.parts, str(p)))
    return matches


def find_documents_dir() -> Path:
    """Resolve Timberborn's Documents directory without consulting the cache."""
    env = os.environ.get("TBOT_DOCUMENTS_DIR")
    if env:
        return Path(env)
    native = Path.home() / "Documents" / "Timberborn"
    if native.is_dir():
        return native
    proton_matches = _candidate_proton_paths()
    if proton_matches:
        return proton_matches[0]
    raise TimberbotPathError(
        "could not locate Timberborn Documents directory; set TBOT_DOCUMENTS_DIR "
        "or pass --documents-dir to point at it."
    )


def documents_dir() -> Path:
    """Timberborn's per-user data root.

    Resolution order: explicit override → cached resolver result →
    `find_documents_dir()`. The resolver is called at most once per process
    (or until `reset_cache()` clears the cache).
    """
    global _documents_dir_cached
    if _documents_dir_override is not None:
        return _documents_dir_override
    if _documents_dir_cached is not None:
        return _documents_dir_cached
    _documents_dir_cached = find_documents_dir()
    return _documents_dir_cached


def mod_dir() -> Path:
    """The Timberbot mod folder.

    Precedence: explicit override → `TBOT_MOD_DIR` env var →
    `documents_dir() / "Mods" / "Timberbot"`. The env var sits at the same
    tier as `TBOT_DOCUMENTS_DIR` and lets users on unusual Wine prefixes pin
    the mod folder without also setting Documents.
    """
    if _mod_dir_override is not None:
        return _mod_dir_override
    env = os.environ.get("TBOT_MOD_DIR")
    if env:
        return Path(env)
    return documents_dir() / "Mods" / "Timberbot"


def settings_path() -> Path:
    """The mod's settings.json file."""
    return mod_dir() / "settings.json"


def saves_dir() -> Path:
    """Per-settlement save directory under `documents_dir()`."""
    return documents_dir() / "Saves"


def memory_base() -> Path:
    """Root of per-settlement brain.toon storage."""
    return mod_dir() / "memory"


_FS_BAD = re.compile(r'[<>:"/\\|?*]')


def sanitize_name(name: str) -> str:
    """Make a settlement name filesystem-safe; never returns empty."""
    return _FS_BAD.sub("_", name).strip() or "unknown"
