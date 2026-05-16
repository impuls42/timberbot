"""`tbot launch settlement:<name>` — pass `--tb-*` args to Steam.

Per-platform launch strategy:

* **Windows.** `taskkill` any running Timberborn.exe, then start Steam with
  `-applaunch 1062090 --tb-settlement <name> [--tb-save <save>]`. Wait for the
  process to come back up via `tasklist`.
* **Linux/Proton & Steam Deck.** `pkill -f Timberborn` then
  `steam -applaunch 1062090 --tb-settlement <name> ...` when `steam` is on
  PATH; fall back to `xdg-open "steam://rungameid/1062090//<url-encoded args>/"`
  otherwise.
* **macOS.** No headless launch (Steam URL-handler launch isn't reliable from
  a CLI process). Print a copy-paste-ready Steam launch-options string and
  ask the user to open the game manually.

The mod's `TimberbotAutoLoad` reads these argv flags case-insensitively, with
the value as the next argv slot (space-separated). When `--tb-save` is
omitted, the mod auto-picks the most recent save in the settlement.
"""
from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.parse

from timberbot.api.client import TimberbotClient
from timberbot.formatters.colors import BGRN, BOLD, DIM, RED, RST

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


def _build_extra_args(settlement: str, save_name: str | None) -> list[str]:
    args = ["--tb-settlement", settlement]
    if save_name:
        # the mod's TimberbotAutoLoad strips `.timber` itself, but accepting it
        # here too keeps the historical behaviour of `tbot launch save:Foo.timber`
        if save_name.endswith(".timber"):
            save_name = save_name[:-7]
        args += ["--tb-save", save_name]
    return args


def _build_steam_url(extra_args: list[str]) -> str:
    """`steam://rungameid/<appid>//<url-encoded args>/` form for URL-handler launches."""
    joined = " ".join(extra_args)
    encoded = urllib.parse.quote(joined, safe="")
    return f"steam://rungameid/{TIMBERBORN_APPID}//{encoded}/"


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


def _windows_kill_and_launch(extra_args: list[str]) -> bool:
    """Kill any running Timberborn.exe and start it via Steam with the given
    extra args. Returns whether the process is up afterwards."""
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
        subprocess.Popen([steam_exe, "-applaunch", TIMBERBORN_APPID, *extra_args])
    else:
        url = _build_steam_url(extra_args)
        subprocess.Popen(["cmd.exe", "/c", "start", url], shell=False)

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


def _linux_proton_kill_and_launch(extra_args: list[str]) -> bool:
    """Kill any running Timberborn (Proton wraps it as `Timberborn.exe` under
    Wine) and ask Steam to relaunch it with the given extra args. Returns
    whether the process comes back up.

    When `steam` is on PATH we use `-applaunch <appid> <args>`; falling back to
    `xdg-open` requires URL-encoding the args into the `steam://rungameid/...`
    form.
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

    steam = shutil.which("steam")
    if steam is not None:
        subprocess.Popen([steam, "-applaunch", TIMBERBORN_APPID, *extra_args])
    else:
        xdg = shutil.which("xdg-open")
        if xdg is None:
            print(
                f"  {RED}error: no steam or xdg-open on PATH; cannot launch Timberborn{RST}",
                file=sys.stderr,
            )
            return False
        subprocess.Popen([xdg, _build_steam_url(extra_args)])

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

    extra_args = _build_extra_args(settlement, save_name)
    label = f"{settlement} / {save_name}" if save_name else f"{settlement} (most recent save)"
    print(f"  {BOLD}launching{RST} {label}")

    if platform.system() == "Darwin":
        opts = " ".join(extra_args)
        print(f"  {BGRN}args ready{RST}  Steam launch options: {opts}")
        print(f"  {DIM}macOS: set those in Steam → Timberborn → Properties → Launch Options, then open the game.{RST}")
        print(f"  {DIM}or run: open -a Steam --args -applaunch {TIMBERBORN_APPID} {opts}{RST}")
        return 0

    if sys.platform == "win32":
        started = _windows_kill_and_launch(extra_args)
    else:
        started = _linux_proton_kill_and_launch(extra_args)

    if not started:
        print(
            f"  {RED}error: Timberborn did not start after the wait window. "
            f"Is Steam running?{RST}",
            file=sys.stderr,
        )
        return 1

    return _wait_for_api(timeout, settlement)
