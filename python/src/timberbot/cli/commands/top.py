"""`tbot top` — live colony dashboard."""
from __future__ import annotations

import contextlib
import sys
import time
from typing import Any

from timberbot.api.client import TimberbotClient
from timberbot.formatters.colors import DIM, RED, RST
from timberbot.formatters.dashboard import render_top


def _drain_key(is_win: bool, msvcrt_mod: object | None, select_mod: object | None) -> bytes | None:
    if is_win:
        if msvcrt_mod is not None and msvcrt_mod.kbhit():  # type: ignore[attr-defined]
            return msvcrt_mod.getch()  # type: ignore[attr-defined]
        return None
    if select_mod is None:
        return None
    rlist, _, _ = select_mod.select([sys.stdin], [], [], 0)  # type: ignore[attr-defined]
    if rlist:
        ch = sys.stdin.read(1)
        return ch.encode("utf-8")
    return None


def top(interval: int = 5) -> int:
    """Live colony dashboard. Tick every `interval` seconds. Press q to quit, 0-3 to set game speed."""
    is_win = sys.platform == "win32"
    msvcrt_mod = None
    select_mod = None
    tty_mod = None
    termios_mod = None
    if is_win:
        import msvcrt as msvcrt_mod  # type: ignore[no-redef]
    else:
        import select as select_mod  # type: ignore[no-redef]
        import termios as termios_mod  # type: ignore[no-redef]
        import tty as tty_mod  # type: ignore[no-redef]

    bot = TimberbotClient(json_mode=True)
    if not bot.ping():
        print(f"  {RED}cannot reach Timberbot on port 8085{RST}")
        print(f"  {DIM}start Timberborn with the mod loaded{RST}\n")
        return 1

    old_settings = None
    if not is_win and termios_mod is not None and tty_mod is not None:
        old_settings = termios_mod.tcgetattr(sys.stdin)
        tty_mod.setcbreak(sys.stdin.fileno())

    try:
        while True:
            summary_dict: dict[str, Any] | None
            try:
                summary_dict = bot.summary().model_dump(exclude_none=True)
            except Exception:
                summary_dict = None
            print("\033[2J\033[H", end="")
            print()
            print(render_top(summary_dict, interval=interval))

            deadline = time.time() + interval
            while time.time() < deadline:
                key = _drain_key(is_win, msvcrt_mod, select_mod)
                if key is None:
                    time.sleep(0.1)
                    continue
                ch = key.lower()
                if ch == b"q":
                    print(f"\n  {DIM}bye!{RST}\n")
                    return 0
                if ch in (b"0", b"1", b"2", b"3"):
                    with contextlib.suppress(Exception):
                        bot.set_speed(int(ch))
                    break
    except KeyboardInterrupt:
        print(f"\n  {DIM}bye!{RST}\n")
    finally:
        if old_settings is not None and not is_win and termios_mod is not None:
            termios_mod.tcsetattr(sys.stdin, termios_mod.TCSADRAIN, old_settings)
    return 0
