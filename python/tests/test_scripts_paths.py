"""Smoke test for `scripts/_paths.py` — the standalone build-time resolver.

The resolver lives outside `python/src/timberbot/` because the runtime `tbot`
CLI no longer touches the game's Documents directory (impuls42/timberbot#43
PR 4). `scripts/deploy.sh` is its only consumer.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "_paths.py"


def test_script_exists():
    assert SCRIPT.is_file()


def test_script_respects_tbot_documents_dir(tmp_path):
    env = dict(os.environ)
    env["TBOT_DOCUMENTS_DIR"] = str(tmp_path / "FakeBorn")
    env.pop("TBOT_MOD_DIR", None)
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / "FakeBorn" / "Mods" / "Timberbot")


def test_script_respects_tbot_mod_dir(tmp_path):
    env = dict(os.environ)
    env["TBOT_DOCUMENTS_DIR"] = str(tmp_path / "FakeBorn")
    env["TBOT_MOD_DIR"] = str(tmp_path / "custom-mods")
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / "custom-mods")


def test_script_fails_clearly_when_unresolvable(tmp_path, monkeypatch):
    """No env var, no `~/Documents/Timberborn`, no Proton compatdata → exit 1."""
    env = dict(os.environ)
    env.pop("TBOT_DOCUMENTS_DIR", None)
    env.pop("TBOT_MOD_DIR", None)
    # Point HOME at an empty tmp so neither the native ~/Documents/Timberborn
    # branch nor any `.steam` compatdata can match.
    env["HOME"] = str(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert "TBOT_DOCUMENTS_DIR" in r.stderr
