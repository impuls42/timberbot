"""`tbot launch settlement:<name>` — write autoload.json and (on Windows) launch Timberborn.

Linux/Proton support arrives in PR 4.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from tbot.api.client import TimberbotClient
from tbot.formatters.colors import BGRN, BOLD, DIM, RED, RST
from tbot.paths import mod_dir, saves_dir


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

    if platform.system() == "Darwin":
        print(f"  {BGRN}autoload prepared{RST}  {settlement} / {save_name}")
        print(f"  {DIM}open Timberborn manually on macOS and the mod will load this save from autoload.json{RST}")
        return 0

    # Windows-only launch path; Linux/Proton arrives in PR 4.
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

    print(f"  {BOLD}launching{RST} {settlement} / {save_name}")
    steam_exe = r"C:\Games\Steam\steam.exe"
    if os.path.exists(steam_exe):
        subprocess.Popen([steam_exe, "-applaunch", "1062090"])
    else:
        subprocess.Popen(["cmd.exe", "/c", "start", "steam://rungameid/1062090"], shell=False)

    print(f"  {DIM}waiting for Timberborn.exe to start...{RST}")
    exe_started = False
    for _ in range(30):
        time.sleep(2)
        check = subprocess.run(
            ["tasklist", "/fi", "imagename eq Timberborn.exe"],
            capture_output=True, text=True,
        )
        if "Timberborn.exe" in check.stdout:
            exe_started = True
            break
    if not exe_started:
        print(f"  {RED}error: Timberborn.exe did not start after 60s. Is Steam running?{RST}", file=sys.stderr)
        return 1

    return _wait_for_api(timeout, settlement)
