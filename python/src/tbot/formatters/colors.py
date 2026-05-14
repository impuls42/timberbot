"""ANSI color/style constants used by the formatters and CLI.

Pure module-level strings; no behavior. The CLI is responsible for ensuring the
output stream supports them (see `tbot.cli.main` for the Windows UTF-8 fix-up).
"""
from __future__ import annotations

RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

RED = "\033[31m"
GRN = "\033[32m"
YEL = "\033[33m"
BLU = "\033[34m"
MAG = "\033[35m"
CYN = "\033[36m"
WHT = "\033[37m"

BRED = "\033[91m"
BGRN = "\033[92m"
BYEL = "\033[93m"
BBLU = "\033[94m"
BMAG = "\033[95m"
BCYN = "\033[96m"
BWHT = "\033[97m"
