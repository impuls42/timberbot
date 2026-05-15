"""Top-level pytest fixtures.

CI runs without a Timberborn install, so `timberbot.paths.find_documents_dir`
would raise `TimberbotPathError` for every test that constructs a
`TimberbotClient` (which transitively builds a `SettlementContext`). Point
`TBOT_DOCUMENTS_DIR` at a session-wide tmp dir so the resolver always
succeeds in tests. Tests that care about the resolver itself
(`test_paths.py`, `test_proton_paths.py`) override or delete the env var in
their own per-test fixtures, so this default is harmless for them.
"""
from __future__ import annotations

import os
from pathlib import Path


def pytest_configure(config):
    if "TBOT_DOCUMENTS_DIR" not in os.environ:
        # tmp_path_factory isn't accessible from pytest_configure, so use a
        # plain tmp dir keyed on pid. The dir is small (we never write to it
        # in the bulk of tests; tests that do write set their own subdir).
        base = Path(os.environ.get("PYTEST_TMPDIR") or "/tmp") / f"tbot-{os.getpid()}"
        base.mkdir(parents=True, exist_ok=True)
        os.environ["TBOT_DOCUMENTS_DIR"] = str(base)
