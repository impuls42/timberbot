"""ASCII map renderer.

Pure: takes a `/api/tiles` response plus bounding rect and returns a string.
The legacy `Timberbot.map()` printed inside the client; the CLI now owns I/O.
"""
from __future__ import annotations

from typing import Any

from tbot.formatters.colors import (
    BBLU,
    BGRN,
    BLU,
    BMAG,
    BOLD,
    BWHT,
    BYEL,
    CYN,
    DIM,
    GRN,
    MAG,
    RED,
    RST,
    YEL,
)

# Symbol + color per occupant prefix. Resolved by case-insensitive substring
# match against the occupant name; first match wins.
STYLE: dict[str, tuple[str, str]] = {
    "Path": ("=", YEL),
    "DistrictCenter": ("D", BOLD + BYEL),
    "Rowhouse": ("H", YEL), "Barrack": ("H", YEL), "Lodge": ("H", YEL),
    "Breeding": ("R", YEL),
    "LumberMill": ("M", BWHT), "WoodWorkshop": ("M", BWHT),
    "IndustrialLumberMill": ("M", BWHT),
    "FarmHouse": ("F", CYN), "Forester": ("f", GRN),
    "PowerWheel": ("E", BYEL), "PowerShaft": ("E", BYEL),
    "Inventor": ("S", BWHT), "Numbercruncher": ("S", BWHT),
    "Lumberjack": ("L", RED), "Gatherer": ("G", MAG),
    "Hauling": ("K", RED), "Scavenger": ("G", RED),
    "Pump": ("P", BBLU), "Tank": ("W", BBLU),
    "Floodgate": ("X", CYN), "Dam": ("X", CYN),
    "Levee": ("X", CYN), "Sluice": ("X", CYN),
    "Warehouse": ("$", YEL), "Pile": ("$", YEL),
    "Pine": ("T", GRN), "Birch": ("T", GRN), "Oak": ("T", GRN),
    "Maple": ("T", GRN), "Chestnut": ("T", GRN),
    "Bush": ("B", MAG), "berry": ("B", MAG),
    "Kohlrabi": ("k", BGRN), "Carrot": ("c", BGRN),
    "Potato": ("p", BGRN), "Wheat": ("w", BGRN),
    "Cassava": ("a", BGRN), "Sunflower": ("s", BGRN),
    "Corn": ("n", BGRN), "Eggplant": ("e", BGRN),
    "Cattail": ("l", BGRN), "Spadderdock": ("d", BGRN),
    "Soybean": ("y", BGRN), "Canola": ("o", BGRN),
    "Campfire": ("C", RED),
    "Stairs": ("/", YEL), "Platform": ("_", YEL),
    "Metalsmith": ("m", BWHT), "Smelter": ("m", BWHT),
    "GearWorkshop": ("g", BWHT),
    "BotAssembler": ("b", BMAG), "BotPartFactory": ("b", BMAG),
    "ChargingStation": ("z", BMAG),
    "FluidDump": ("V", BBLU), "DoubleShower": ("v", BBLU),
    "SwimmingPool": ("v", BBLU),
    "Scratcher": ("~", GRN), "Bench": ("~", GRN),
    "ExercisePlaza": ("~", GRN), "MedicalBed": ("~", GRN),
    "Brazier": ("*", RED), "Lantern": ("*", YEL),
    "BeaverBust": ("*", YEL), "Roof": ("^", DIM),
    "Ruin": ("R", DIM), "Relic": ("R", DIM),
    "FoodFactory": ("F", CYN),
    "Slope": ("/", DIM),
    "AncientAquiferDrill": ("A", BBLU),
    "Shrub": ("B", MAG), "Geothermal": ("G", RED),
    "CompactWaterWheel": ("P", BBLU), "LargeWaterWheel": ("P", BBLU),
    "BadwaterDischarge": ("V", BBLU), "Centrifuge": ("V", BBLU),
    "Valve": ("X", CYN), "FillValve": ("X", CYN),
    "AquiferDrill": ("A", BBLU), "IrrigationBarrier": ("X", CYN),
    "SteamEngine": ("E", BYEL), "GravityBattery": ("E", BYEL),
    "Clutch": ("E", BYEL),
    "CoffeeBrewery": ("F", CYN), "OilPress": ("F", CYN),
    "Fermenter": ("F", CYN), "TappersShack": ("F", CYN),
    "ExplosivesFactory": ("F", CYN), "HydroponicGarden": ("F", CYN),
    "EfficientMine": ("F", CYN), "GreaseFactory": ("F", CYN),
    "Detailer": ("~", GRN), "MudBath": ("~", GRN),
    "WindTunnel": ("~", GRN), "Motivatorium": ("~", GRN),
    "TeethGrindstone": ("~", GRN), "DecontaminationPod": ("~", GRN),
    "BeaverStatue": ("*", YEL), "Bell": ("*", YEL),
    "DecorativeClock": ("*", YEL), "MetalFence": ("|", DIM),
    "WoodFence": ("|", DIM), "PoleBanner": ("!", YEL),
    "SquareBanner": ("!", YEL), "FireworkLauncher": ("!", YEL),
    "StreamGauge": ("*", DIM),
    "Gate": ("=", YEL), "Tunnel": ("=", YEL),
    "DistrictCrossing": ("=", YEL),
    "Tubeway": ("=", BMAG), "TubewayStation": ("=", BMAG),
    "VerticalTubeway": ("=", BMAG),
    "SuspensionBridge": ("=", YEL), "Overhang": ("_", DIM),
    "ImpermeableFloor": ("_", DIM), "TerrainBlock": ("#", DIM),
    "DirtExcavator": ("#", DIM),
    "Lever": ("i", DIM), "Sensor": ("i", DIM), "Timer": ("i", DIM),
    "Memory": ("i", DIM), "Relay": ("i", DIM), "Indicator": ("i", DIM),
    "Speaker": ("i", DIM), "HttpAdapter": ("i", DIM), "HttpLever": ("i", DIM),
    "Chronometer": ("i", DIM), "Counter": ("i", DIM),
    "WeatherStation": ("i", DIM), "PowerMeter": ("i", DIM),
    "LaborerMonument": ("Q", BYEL), "FlameOfUnity": ("Q", BYEL),
    "TributeToIngenuity": ("Q", BYEL), "EarthRepopulator": ("Q", BYEL),
    "Dynamite": ("x", RED), "DoubleDynamite": ("x", RED),
    "TripleDynamite": ("x", RED), "Detonator": ("x", RED),
    "BuildersHut": ("K", RED), "ControlTower": ("b", BMAG),
}


def _zbg(z: int) -> str:
    """Background-color escape per terrain elevation."""
    if z < 10:
        shade = 234 + z
    elif z < 20:
        shade = 244 + (z - 10)
    else:
        shade = 254 + min(z - 20, 1)
    return f"\033[48;5;{min(shade, 255)}m"


def render_map(tiles_response: dict[str, Any], x1: int, y1: int, x2: int, y2: int) -> str:
    """Render a colored ASCII map of the given bounding box.

    `tiles_response` is the raw JSON from `/api/tiles` (with `format=json`).
    Returns a single string with one line per row plus an axis and legend at
    the bottom; the caller is responsible for printing it.
    """
    tiles = {(t["x"], t["y"]): t for t in tiles_response.get("tiles", [])}
    legend: dict[str, tuple[str, str]] = {}
    z_levels: set[int] = set()

    lines: list[str] = []
    for ty in range(y2, y1 - 1, -1):
        line = f"{DIM}{ty:3d}{RST} "
        pbg = pco = ""
        for tx in range(x1, x2 + 1):
            t = tiles.get((tx, ty))
            if not t:
                if pbg or pco:
                    line += RST
                    pbg = pco = ""
                line += f"{DIM}?{RST}"
                continue
            occ = t.get("occupants")
            occupant = max(occ, key=lambda o: o["z"])["name"] if occ else None
            entrance = t.get("entrance", False)
            bg = co = ch = None
            if entrance and not occupant:
                bg = _zbg(t["terrain"])
                z_levels.add(t["terrain"])
                co = BWHT
                ch = "@"
                legend["@"] = (BWHT, "entrance")
            elif occupant:
                bg = _zbg(t["terrain"])
                z_levels.add(t["terrain"])
                for key, (c, s) in STYLE.items():
                    if key.lower() in occupant.lower():
                        ch, co = c, s
                        legend[c] = (s, key)
                        break
                if ch == "T" and t.get("seedling"):
                    ch, co = "t", DIM + GRN
                    legend["t"] = (co, "seedling")
                if not ch:
                    ch = occupant[0]
                    co = DIM
                    legend[ch] = (DIM, occupant)
            elif t.get("water", 0) > 0:
                bg = _zbg(t["terrain"])
                z_levels.add(t["terrain"])
                co = BLU
                ch = "~"
                legend["~"] = (BLU, "water")
            elif t["terrain"] > 0:
                bg = _zbg(t["terrain"])
                z_levels.add(t["terrain"])
                ch = str(t["terrain"] % 10)
                co = GRN if t.get("moist") else DIM
            else:
                if pbg or pco:
                    line += RST
                    pbg = pco = ""
                line += " "
                continue
            delta = ""
            if bg != pbg:
                delta += bg or ""
            if co != pco:
                delta += co or ""
            line += delta + ch
            pbg = bg
            pco = co
        if pbg or pco:
            line += RST
        lines.append(line)

    axis = f"    {DIM}" + "".join(str(i % 10) for i in range(x1, x2 + 1)) + RST
    lines.append(axis)

    leg = "  "
    for ch, (co, label) in sorted(legend.items(), key=lambda x: x[1][1]):
        leg += f" {co}{ch}{RST} {label}"
    lines.append(leg)

    if len(z_levels) > 1:
        zleg = "   height:"
        for z in sorted(z_levels):
            zleg += f" {_zbg(z)} z={z} {RST}"
        lines.append(zleg)

    return "\n".join(lines)
