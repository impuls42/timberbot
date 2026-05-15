"""`tbot launch settlement:<name>` — write autoload.json and launch Timberborn.

Per-platform launch strategy:

* **Windows.** `taskkill` any running Timberborn.exe, then start Steam with
  `-applaunch 1062090` (preferred) or `steam://rungameid/1062090`. Wait for
  the process to come back up via `tasklist`.
* **Linux/Proton & Steam Deck.** `pkill -f Timberborn` then
  `steam steam://rungameid/1062090` (or `xdg-open` as a fallback). The mod
  runs inside the Proton prefix; the Documents-dir resolver in
  `timberbot.paths` already knows how to find it.
* **macOS.** No headless launch (Apple doesn't ship Timberborn natively).
  Prepare `autoload.json` and tell the user to open the game manually.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from timberbot.api.client import TimberbotClient
from timberbot.formatters.colors import BGRN, BOLD, DIM, RED, RST
from timberbot.paths import mod_dir, saves_dir

TIMBERBORN_APPID = "1062090"


def _parse_args(args: list[str]) -> tuple[str | None, str | None, int]:
    settlement: str | None = None
    save_name: str | None = None
    timeout = 120
    for a in args:
        if ":" not in a:
            continue
        key, val = a.split(":", 1)
        if key == "settlement":
            settlement = val
        elif key == "save":
            save_name = val
        elif key == "timeout":
            with contextlib.suppress(ValueError):
                timeout = int(val)
    return settlement, save_name, timeout


def _resolve_save(sdir: Path, save_name: str | None) -> str:
    if save_name:
        if save_name.endswith(".timber"):
            save_name = save_name[:-7]
        spath = sdir / f"{save_name}.timber"
        if not spath.is_file():
            print(f"  {RED}error: save not found: {spath}{RST}", file=sys.stderr)
            sys.exit(1)
        return save_name
    timbers = [f for f in os.listdir(sdir) if f.endswith(".timber")]
    if not timbers:
        print(f"  {RED}error: no saves in {sdir}{RST}", file=sys.stderr)
        sys.exit(1)
    timbers.sort(key=lambda f: os.path.getmtime(sdir / f), reverse=True)
    return timbers[0][:-7]


def _wait_for_api(timeout: int, settlement: str) -> int:
    print(f"  {DIM}waiting for game to load (timeout {timeout}s)...{RST}")
    start = time.time()
    bot = TimberbotClient(json_mode=True)
    while time.time() - start < timeout:
        try:
            s = bot.summary()
            name = ""
            for d in s.districts:
                if d.name:
                    name = d.name
                    break
            print(f"  {BGRN}ready{RST}  settlement: {name or settlement}")
            return 0
        except Exception:
            time.sleep(3)
    print(f"  {RED}timeout after {timeout}s. game may still be loading{RST}", file=sys.stderr)
    return 1


def _windows_kill_and_launch() -> bool:
    """Kill any running Timberborn.exe and start it via Steam. Returns whether
    the process is up afterwards."""
    try:
        r = subprocess.run(
            ["taskkill", "/f", "/im", "Timberborn.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if r.returncode == 0:
            print(f"  {DIM}waiting for Timberborn to exit...{RST}")
            for _ in range(30):
                time.sleep(1)
                check = subprocess.run(
                    ["tasklist", "/fi", "imagename eq Timberborn.exe"],
                    capture_output=True, text=True,
                )
                if "Timberborn.exe" not in check.stdout:
                    break
            time.sleep(2)
    except Exception:
        pass

    steam_exe = r"C:\Games\Steam\steam.exe"
    if os.path.exists(steam_exe):
        subprocess.Popen([steam_exe, "-applaunch", TIMBERBORN_APPID])
    else:
        subprocess.Popen(["cmd.exe", "/c", "start", f"steam://rungameid/{TIMBERBORN_APPID}"], shell=False)

    print(f"  {DIM}waiting for Timberborn.exe to start...{RST}")
    for _ in range(30):
        time.sleep(2)
        check = subprocess.run(
            ["tasklist", "/fi", "imagename eq Timberborn.exe"],
            capture_output=True, text=True,
        )
        if "Timberborn.exe" in check.stdout:
            return True
    return False


def _linux_proton_kill_and_launch() -> bool:
    """Kill any running Timberborn (Proton wraps it as `Timberborn.exe` under
    Wine) and ask Steam to relaunch it. Returns whether the process comes back
    up.

    Steam's URL handler is the most portable trigger — `xdg-open` delegates to
    it; calling `steam` directly also works if it's on PATH.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f", "Timberborn"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            subprocess.run(
                ["pkill", "-f", "Timberborn"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"  {DIM}waiting for Timberborn to exit...{RST}")
            for _ in range(30):
                time.sleep(1)
                check = subprocess.run(
                    ["pgrep", "-f", "Timberborn"],
                    capture_output=True, text=True,
                )
                if not check.stdout.strip():
                    break
            time.sleep(2)
    except FileNotFoundError:
        # No `pgrep`/`pkill` — Steam Deck and most distros ship them, but if
        # they're missing, skip the kill and proceed to the launch step.
        pass

    url = f"steam://rungameid/{TIMBERBORN_APPID}"
    launcher = shutil.which("steam") or shutil.which("xdg-open")
    if launcher is None:
        print(
            f"  {RED}error: no steam or xdg-open on PATH; cannot launch Timberborn{RST}",
            file=sys.stderr,
        )
        return False
    subprocess.Popen([launcher, url])

    print(f"  {DIM}waiting for Timberborn to start...{RST}")
    for _ in range(45):
        time.sleep(2)
        check = subprocess.run(
            ["pgrep", "-f", "Timberborn"],
            capture_output=True, text=True,
        )
        if check.stdout.strip():
            return True
    return False


def run(args: list[str]) -> int:
    settlement, save_name, timeout = _parse_args(args)
    if not settlement:
        print(f"  {RED}error: settlement:<name> is required{RST}", file=sys.stderr)
        print("  usage: tbot launch settlement:<name> [save:<filename>] [timeout:120]", file=sys.stderr)
        return 1

    saves = saves_dir()
    sdir = saves / settlement
    if not sdir.is_dir():
        print(f"  {RED}error: settlement folder not found: {sdir}{RST}", file=sys.stderr)
        avail = [d for d in os.listdir(saves) if (saves / d).is_dir()] if saves.is_dir() else []
        if avail:
            print(f"  available: {', '.join(sorted(avail))}", file=sys.stderr)
        return 1

    save_name = _resolve_save(sdir, save_name)

    md = mod_dir()
    md.mkdir(parents=True, exist_ok=True)
    with open(md / "autoload.json", "w") as f:
        json.dump({"settlement": settlement, "save": save_name}, f)

    print(f"  {BOLD}launching{RST} {settlement} / {save_name}")

    if platform.system() == "Darwin":
        print(f"  {BGRN}autoload prepared{RST}  {settlement} / {save_name}")
        print(f"  {DIM}open Timberborn manually on macOS and the mod will load this save from autoload.json{RST}")
        return 0

    if sys.platform == "win32":
        started = _windows_kill_and_launch()
    else:
        started = _linux_proton_kill_and_launch()

    if not started:
        print(
            f"  {RED}error: Timberborn did not start after the wait window. "
            f"Is Steam running?{RST}",
            file=sys.stderr,
        )
        return 1

    return _wait_for_api(timeout, settlement)
