"""`tbot manager` — auto-pause low-priority buildings to keep idle haulers in band."""
from __future__ import annotations

import time

from timberbot.api.client import TimberbotClient
from timberbot.api.models._generated import Building
from timberbot.formatters.colors import BGRN, BMAG, BOLD, BRED, BYEL, DIM, RED, RST, YEL

ESSENTIAL = {
    "FarmHouse", "DeepWaterPump", "LumberjackFlag", "ScavengerFlag",
    "GathererFlag", "BreedingPod", "SmallTank", "MediumTank", "LargeTank",
}
LOW_PRIORITY = [
    "Inventor", "Metalsmith", "BotPartFactory", "BotAssembler",
    "GearWorkshop", "Scratcher", "FluidDump", "Forester",
    "IndustrialLumberMill", "LargePowerWheel", "DistrictCenter",
]


def is_essential(name: str) -> bool:
    return any(e in name for e in ESSENTIAL)


def manager(
    *,
    host: str | None = None,
    port: int | None = None,
    auth_token: str | None = None,
) -> int:
    """Auto-pause low-priority buildings to keep idle haulers in band (1-4).

    `host`/`port`/`auth_token` are forwarded from the global `tbot --host=` /
    `--port=` / `--auth-token=` flags by `Tbot.manager`; the public CLI surface
    is `tbot manager` (no args).
    """
    bot = TimberbotClient(host=host, port=port, auth_token=auth_token, json_mode=True)
    if not bot.ping():
        print(f"  {RED}cannot reach Timberbot at {bot.url}{RST}")
        return 1

    print(
        f"  {BOLD}{BMAG}timberbot manage{RST}  "
        f"{DIM}keeping 1-4 idle haulers. ctrl+c to stop{RST}\n"
    )

    paused_by_us: list[int] = []

    try:
        while True:
            try:
                summary = bot.summary()
                idle = sum(
                    (d.employment.unemployed or 0) if d.employment else 0
                    for d in summary.districts
                )
                bldgs = bot.buildings()
                blist: list[Building] = bldgs.items or []
            except Exception:
                print(f"  {RED}-- connection lost --{RST}")
                time.sleep(10)
                continue

            idle_color = BRED if idle == 0 else BGRN if idle <= 4 else BYEL
            ts = time.strftime("%H:%M:%S")

            if idle == 0:
                acted = False
                for prio_name in LOW_PRIORITY:
                    for b in blist:
                        if (
                            prio_name in b.name
                            and not b.paused
                            and (b.assignedWorkers or 0) > 0
                            and not is_essential(b.name)
                        ):
                            bot.pause_building(b.id)
                            paused_by_us.append(b.id)
                            print(
                                f"  {ts}  {BRED}0 idle{RST}  paused "
                                f"{BYEL}{b.name}{RST} id:{b.id}"
                            )
                            acted = True
                            break
                    if acted:
                        break
                if not acted:
                    print(f"  {ts}  {BRED}0 idle{RST}  {DIM}nothing left to pause{RST}")
            elif idle > 4 and paused_by_us:
                bid = paused_by_us.pop()
                name = "?"
                for b in blist:
                    if b.id == bid:
                        name = b.name
                        break
                bot.unpause_building(bid)
                print(
                    f"  {ts}  {YEL}{idle} idle{RST}  unpaused "
                    f"{BGRN}{name}{RST} id:{bid}"
                )
            else:
                print(f"  {ts}  {idle_color}{idle} idle{RST}  {DIM}ok{RST}")

            time.sleep(10)
    except KeyboardInterrupt:
        print(f"\n  {DIM}bye!{RST}\n")
        return 0
    return 0
