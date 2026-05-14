"""`tbot top` — live colony dashboard."""
from __future__ import annotations

import sys
import time

from tbot.api.client import TimberbotClient
from tbot.formatters.colors import DIM, RED, RST
from tbot.formatters.dashboard import render_top


def _drain_key(is_win: bool, msvcrt_mod: object | None, select_mod: object | None) -> bytes | None:
    """Non-blocking single-key read; returns bytes or None."""
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


def _parse_interval(args: list[str], default: int = 5) -> int:
    for a in args:
        if a.startswith("interval:"):
            try:
                return int(a.split(":", 1)[1])
            except ValueError:
                pass
    return default


def run(args: list[str]) -> int:
    interval = _parse_interval(args)
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

    agent_turns = 5
    agent_model = "claude-haiku-4-5-20251001"

    old_settings = None
    if not is_win and termios_mod is not None and tty_mod is not None:
        old_settings = termios_mod.tcgetattr(sys.stdin)
        tty_mod.setcbreak(sys.stdin.fileno())

    try:
        while True:
            try:
                summary = bot.summary()
            except Exception:
                summary = None
            try:
                agent = bot._get_json("/api/agent/status")
            except Exception:
                agent = None
            print("\033[2J\033[H", end="")
            print()
            print(render_top(summary, interval=interval, agent_data=agent, agent_turns=agent_turns))

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
                if ch == b"s":
                    agent_st = agent.get("status") if agent else "idle"
                    if agent_st in ("idle", "done", "error", None):
                        try:
                            bot._post("/api/agent/start", {
                                "binary": "claude", "turns": agent_turns,
                                "model": agent_model, "interval": 5, "timeout": 300,
                            })
                            break
                        except Exception:
                            pass
                elif ch == b"x":
                    try:
                        bot._post("/api/agent/stop", {})
                    except Exception:
                        pass
                    break
                elif ch in (b"+", b"="):
                    agent_turns = min(agent_turns + 5, 100)
                    break
                elif ch == b"-":
                    agent_turns = max(agent_turns - 5, 1)
                    break
                elif ch in (b"0", b"1", b"2", b"3"):
                    try:
                        bot.set_speed(int(ch))
                    except Exception:
                        pass
                    break
    except KeyboardInterrupt:
        print(f"\n  {DIM}bye!{RST}\n")
    finally:
        if old_settings is not None and not is_win and termios_mod is not None:
            termios_mod.tcsetattr(sys.stdin, termios_mod.TCSADRAIN, old_settings)
    return 0
