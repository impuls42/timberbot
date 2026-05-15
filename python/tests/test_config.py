"""Unit tests for the user-config-dir resolver."""
from __future__ import annotations

import sys
from pathlib import Path

from timberbot import config


def test_explicit_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TBOT_CONFIG_DIR", str(tmp_path / "explicit"))
    assert config.config_dir() == tmp_path / "explicit"


def test_linux_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("TBOT_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_dir() == tmp_path / "xdg" / "timberbot"


def test_linux_falls_back_to_dot_config(monkeypatch, tmp_path):
    monkeypatch.delenv("TBOT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.config_dir() == tmp_path / ".config" / "timberbot"


def test_macos_uses_application_support(monkeypatch, tmp_path):
    monkeypatch.delenv("TBOT_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.config_dir() == tmp_path / "Library" / "Application Support" / "timberbot"


def test_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("TBOT_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert config.config_dir() == tmp_path / "Roaming" / "timberbot"


def test_windows_falls_back_to_home_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("TBOT_CONFIG_DIR", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.config_dir() == tmp_path / "AppData" / "Roaming" / "timberbot"
