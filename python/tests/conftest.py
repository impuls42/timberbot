"""Top-level pytest fixtures.

Empty since impuls42/timberbot#43 PR 4 — `tbot` no longer consults the game's
Documents directory at runtime, so the previous session-wide
`TBOT_DOCUMENTS_DIR` shim is unnecessary. Tests that need a writable data
dir for `brain.toon` should set `TBOT_DATA_DIR` (or pass a `base=` override
to `SettlementContext`) themselves.
"""
from __future__ import annotations
