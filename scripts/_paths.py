#!/usr/bin/env python3
"""Standalone Documents/Mods resolver for `scripts/deploy.sh`.

Build-time helper for the C# Deploy target: prints the path of the
Timberbot mod directory so `dotnet build -p:ModDir=…` can copy the freshly
built DLL into the right place. This is the sole remaining consumer of
Proton/Wine autodiscovery — the runtime `tbot` CLI no longer touches the
game's Documents tree at all (impuls42/timberbot#43 PR 4).

Resolution order (mirrors the pre-#43 `timberbot.paths` behaviour):

1. `TBOT_DOCUMENTS_DIR` env var, if set. No existence check.
2. `~/Documents/Timberborn`, if it exists. Matches the native Windows /
   macOS / Linux install layout.
3. On Linux only, a scan of Proton/Wine compatdata for a Timberborn
   `Documents` directory. The Timberborn Steam AppID is preferred, but any
   matching compatdata is accepted as a fallback so beta branches still
   resolve.
4. Otherwise, raises `SystemExit` with a non-zero code so the shell wrapper
   surfaces a clear error.

`TBOT_MOD_DIR` overrides the mod-folder step independently (same precedence
tier as `TBOT_DOCUMENTS_DIR`). With nothing set, the mod folder is
`<documents_dir>/Mods/Timberbot/`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Timberborn's Steam AppID. Used to prefer the canonical Proton prefix when
# multiple compatdata directories contain a Timberborn folder.
_TIMBERBORN_APPID = "1062090"


class TimberbotPathError(RuntimeError):
    """Raised when Timberborn's Documents directory cannot be located."""


def _candidate_proton_paths() -> list[Path]:
    """Enumerate Proton/Wine compatdata locations that contain a Timberborn dir.

    Limitation: this scan assumes the standard Proton-managed username
    (`steamuser`). Custom Wine prefixes with a different Windows username
    won't be discovered — those setups should set `TBOT_DOCUMENTS_DIR`.
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


def documents_dir() -> Path:
    """Resolve Timberborn's Documents directory or raise `TimberbotPathError`."""
    env = os.environ.get("TBOT_DOCUMENTS_DIR")
    if env:
        return Path(env)
    native = Path.home() / "Documents" / "Timberborn"
    if native.is_dir():
        return native
    matches = _candidate_proton_paths()
    if matches:
        return matches[0]
    raise TimberbotPathError(
        "could not locate Timberborn Documents directory; set TBOT_DOCUMENTS_DIR "
        "or pass an explicit ModDir to scripts/deploy.sh."
    )


def mod_dir() -> Path:
    """Resolve the Timberbot mod folder.

    Precedence: `TBOT_MOD_DIR` env var → `documents_dir() / "Mods" / "Timberbot"`.
    """
    env = os.environ.get("TBOT_MOD_DIR")
    if env:
        return Path(env)
    return documents_dir() / "Mods" / "Timberbot"


if __name__ == "__main__":
    try:
        print(mod_dir())
    except TimberbotPathError as e:
        print(f"_paths.py: {e}", file=sys.stderr)
        sys.exit(1)
