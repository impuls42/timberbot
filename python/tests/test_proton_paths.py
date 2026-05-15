"""Tests for the find_documents_dir resolver, including Proton/Wine scanning."""
from __future__ import annotations

from pathlib import Path

import pytest

from timberbot import paths
from timberbot.paths import TimberbotPathError


@pytest.fixture(autouse=True)
def _reset_paths_cache(monkeypatch):
    paths.reset_cache()
    monkeypatch.delenv("TBOT_DOCUMENTS_DIR", raising=False)
    yield
    paths.reset_cache()


def _isolated_home(monkeypatch, tmp_path) -> Path:
    """Point `Path.home()` and `$HOME` at a fresh empty directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    return home


def _make_proton_dir(home: Path, appid: str, *, my_documents: bool = False) -> Path:
    docs = "My Documents" if my_documents else "Documents"
    target = (
        home / ".steam" / "steam" / "steamapps" / "compatdata" / appid
        / "pfx" / "drive_c" / "users" / "steamuser" / docs / "Timberborn"
    )
    target.mkdir(parents=True)
    return target


def test_find_documents_dir_env_override_wins(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("TBOT_DOCUMENTS_DIR", "/does/not/exist/yet")
    assert paths.find_documents_dir() == Path("/does/not/exist/yet")


def test_find_documents_dir_native_when_present(monkeypatch, tmp_path):
    home = _isolated_home(monkeypatch, tmp_path)
    native = home / "Documents" / "Timberborn"
    native.mkdir(parents=True)
    assert paths.find_documents_dir() == native


def test_find_documents_dir_proton_scan(monkeypatch, tmp_path):
    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    proton = _make_proton_dir(home, "1062090")
    assert paths.find_documents_dir() == proton


def test_find_documents_dir_proton_handles_my_documents(monkeypatch, tmp_path):
    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    proton = _make_proton_dir(home, "1062090", my_documents=True)
    assert paths.find_documents_dir() == proton


def test_find_documents_dir_prefers_timberborn_appid(monkeypatch, tmp_path):
    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    other = _make_proton_dir(home, "9999")
    canonical = _make_proton_dir(home, "1062090")
    # Both exist; the resolver prefers 1062090 regardless of iteration order.
    assert paths.find_documents_dir() == canonical
    assert other.is_dir()  # not mutated


def test_find_documents_dir_raises_when_nothing(monkeypatch, tmp_path):
    _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    with pytest.raises(TimberbotPathError, match="TBOT_DOCUMENTS_DIR"):
        paths.find_documents_dir()


def test_documents_dir_caches_resolution(monkeypatch, tmp_path):
    home = _isolated_home(monkeypatch, tmp_path)
    native = home / "Documents" / "Timberborn"
    native.mkdir(parents=True)
    assert paths.documents_dir() == native
    # Even after the directory disappears, the cached value stays.
    native.rmdir()
    assert paths.documents_dir() == native
