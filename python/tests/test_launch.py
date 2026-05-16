"""Tests for `tbot launch` — Steam-args path, no filesystem coupling.

`launch.run` invokes `subprocess.Popen` to start Steam; these tests intercept
that call and assert the argv the user-facing command emits, including the
URL-encoded fallback path when only `xdg-open` is available.
"""
from __future__ import annotations

import sys

import pytest

from timberbot.cli.commands import launch


@pytest.fixture
def fake_subproc(monkeypatch):
    """Capture Popen + run calls; short-circuit timing and Timberborn detection."""
    popen_calls: list[list[str]] = []
    run_calls: list[list[str]] = []

    class _Proc:
        def __init__(self, args, **_kw):
            popen_calls.append(list(args))

    class _Run:
        def __init__(self, stdout: str = "", returncode: int = 1):
            self.stdout = stdout
            self.returncode = returncode

    def _fake_run(args, **_kw):
        run_calls.append(list(args))
        # First `tasklist`/`pgrep` after the Popen should report the process is up
        # so the wait loop exits immediately.
        if args and args[0] in ("tasklist", "pgrep"):
            return _Run(stdout="Timberborn.exe\n", returncode=0)
        return _Run()

    monkeypatch.setattr(launch.subprocess, "Popen", _Proc)
    monkeypatch.setattr(launch.subprocess, "run", _fake_run)
    monkeypatch.setattr(launch.time, "sleep", lambda _s: None)
    # Skip the post-launch API wait — `tbot launch` returns once Steam reports
    # the process is up; the wait is covered separately by integration tests.
    monkeypatch.setattr(launch, "_wait_for_api", lambda _t, _s: 0)
    return popen_calls, run_calls


def test_linux_steam_settlement_only(monkeypatch, fake_subproc):
    """settlement only → `steam -applaunch 1062090 --tb-settlement <name>`."""
    popen_calls, _ = fake_subproc
    monkeypatch.setattr(launch.sys, "platform", "linux")
    monkeypatch.setattr(launch.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launch.shutil, "which", lambda _b: "/usr/bin/steam")

    rc = launch.run(["settlement:MyCity"])

    assert rc == 0
    assert popen_calls[0] == [
        "/usr/bin/steam", "-applaunch", "1062090", "--tb-settlement", "MyCity",
    ]


def test_linux_steam_settlement_and_save(monkeypatch, fake_subproc):
    """save:<name> appends `--tb-save <name>` (strips trailing `.timber`)."""
    popen_calls, _ = fake_subproc
    monkeypatch.setattr(launch.sys, "platform", "linux")
    monkeypatch.setattr(launch.platform, "system", lambda: "Linux")
    monkeypatch.setattr(launch.shutil, "which", lambda _b: "/usr/bin/steam")

    rc = launch.run(["settlement:MyCity", "save:MyCity (3).timber"])

    assert rc == 0
    assert popen_calls[0] == [
        "/usr/bin/steam", "-applaunch", "1062090",
        "--tb-settlement", "MyCity",
        "--tb-save", "MyCity (3)",
    ]


def test_linux_xdg_open_fallback_url_encodes(monkeypatch, fake_subproc):
    """No `steam` on PATH → xdg-open with URL-encoded args in steam://rungameid form."""
    popen_calls, _ = fake_subproc
    monkeypatch.setattr(launch.sys, "platform", "linux")
    monkeypatch.setattr(launch.platform, "system", lambda: "Linux")

    def _which(b):
        return "/usr/bin/xdg-open" if b == "xdg-open" else None
    monkeypatch.setattr(launch.shutil, "which", _which)

    rc = launch.run(["settlement:My City", "save:Foo Bar"])

    assert rc == 0
    assert popen_calls[0][0] == "/usr/bin/xdg-open"
    url = popen_calls[0][1]
    assert url.startswith("steam://rungameid/1062090//")
    assert url.endswith("/")
    # No `=` form anywhere; args are URL-encoded (spaces → %20, not `+`)
    assert "=" not in url
    assert "%20" in url
    # Both flags present in encoded form
    assert "--tb-settlement" in urllib_unquote(url)
    assert "--tb-save" in urllib_unquote(url)
    assert "My City" in urllib_unquote(url)
    assert "Foo Bar" in urllib_unquote(url)


def test_windows_steam_exe(monkeypatch, fake_subproc):
    """Windows path: steam.exe receives args after -applaunch."""
    popen_calls, _ = fake_subproc
    monkeypatch.setattr(launch.sys, "platform", "win32")
    monkeypatch.setattr(launch.platform, "system", lambda: "Windows")
    monkeypatch.setattr(launch.os.path, "exists", lambda _p: True)

    rc = launch.run(["settlement:MyCity"])

    assert rc == 0
    # First Popen is the launch (the kill-and-wait phase uses subprocess.run)
    assert popen_calls[0][0].endswith("steam.exe")
    assert popen_calls[0][1:] == ["-applaunch", "1062090", "--tb-settlement", "MyCity"]


def test_macos_prints_args_and_returns(monkeypatch, fake_subproc, capsys):
    """macOS branch never starts Steam; just prints the launch-options string."""
    popen_calls, _ = fake_subproc
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    monkeypatch.setattr(launch.platform, "system", lambda: "Darwin")

    rc = launch.run(["settlement:MyCity", "save:MyCity (3)"])

    assert rc == 0
    assert popen_calls == []
    out = capsys.readouterr().out
    assert "--tb-settlement MyCity" in out
    assert "--tb-save MyCity (3)" in out


def test_missing_settlement_errors(monkeypatch, fake_subproc, capsys):
    monkeypatch.setattr(launch.sys, "platform", "linux")
    rc = launch.run([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "settlement:<name> is required" in err


def urllib_unquote(s: str) -> str:
    """Small helper to keep the test imports tidy."""
    import urllib.parse
    return urllib.parse.unquote(s)
