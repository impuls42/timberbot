"""Tests for `--documents-dir` / `--mod-dir` propagation from `tbot` CLI to paths."""
from __future__ import annotations

import pytest

from timberbot import paths
from timberbot.cli.main import main


@pytest.fixture(autouse=True)
def _reset_paths_cache(monkeypatch):
    paths.reset_cache()
    monkeypatch.delenv("TBOT_DOCUMENTS_DIR", raising=False)
    yield
    paths.reset_cache()


def test_documents_dir_flag_sets_mod_dir(capsys):
    # `--help` short-circuits before any TimberbotClient is built, so the
    # override pathway is exercised without needing a live game.
    main(["--documents-dir=/tmp/X", "--help"])
    assert paths.documents_dir() == _path("/tmp/X")
    assert paths.mod_dir() == _path("/tmp/X/Mods/Timberbot")
    capsys.readouterr()  # drop help noise


def test_mod_dir_flag_pins_mod_dir(capsys):
    main(["--documents-dir=/tmp/X", "--mod-dir=/tmp/M", "--help"])
    assert paths.mod_dir() == _path("/tmp/M")
    assert paths.settings_path() == _path("/tmp/M/settings.json")
    capsys.readouterr()


def _path(s: str):
    from pathlib import Path
    return Path(s)
