"""Unit tests for global-flag parsing + --help promotion in timberbot.cli.main."""
from __future__ import annotations

from timberbot.cli.main import GlobalFlags, _promote_help, parse_global_flags


def test_parse_picks_up_json_help_host_port():
    flags, rest = parse_global_flags(
        ["--json", "--help", "--host=1.2.3.4", "--port=9001", "summary"]
    )
    assert isinstance(flags, GlobalFlags)
    assert flags.json_mode is True
    assert flags.help_mode is True
    assert flags.host == "1.2.3.4"
    assert flags.port == 9001
    assert flags.auth_token is None
    # `--help` stays in remaining so the Fire pre-processor can promote it;
    # `--json`/`--host=`/`--port=` are consumed.
    assert rest == ["--help", "summary"]


def test_parse_rejects_non_integer_port(capsys):
    """`--port=abc` should error out instead of silently using the default."""
    import pytest
    with pytest.raises(SystemExit) as exc:
        parse_global_flags(["--port=abc", "summary"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not an integer" in err
    assert "abc" in err


def test_parse_picks_up_auth_token():
    flags, rest = parse_global_flags(["--auth-token=s3cret", "summary"])
    assert flags.auth_token == "s3cret"
    assert rest == ["summary"]


def test_parse_auth_token_allows_equals_in_value():
    """Bearer tokens commonly contain '=' (base64 padding). Only split on the first."""
    flags, _ = parse_global_flags(["--auth-token=abc==", "summary"])
    assert flags.auth_token == "abc=="


def test_parse_defaults():
    flags, rest = parse_global_flags(["summary"])
    assert flags.json_mode is False
    assert flags.help_mode is False
    assert flags.host is None
    assert flags.port is None
    assert flags.auth_token is None
    assert flags.verbosity == 0
    assert flags.debug is False
    assert rest == ["summary"]


def test_parse_verbose_short_counts():
    """`-v` increments verbosity by 1, repeats stack."""
    assert parse_global_flags(["summary"])[0].verbosity == 0
    assert parse_global_flags(["-v", "summary"])[0].verbosity == 1
    assert parse_global_flags(["-v", "-v", "summary"])[0].verbosity == 2
    assert parse_global_flags(["-vv", "summary"])[0].verbosity == 2
    assert parse_global_flags(["-vvv", "summary"])[0].verbosity == 3


def test_parse_verbose_long_alias():
    """`--verbose` is the long form of `-v`."""
    assert parse_global_flags(["--verbose", "summary"])[0].verbosity == 1
    assert parse_global_flags(["--verbose", "-v", "summary"])[0].verbosity == 2


def test_parse_debug():
    """`--debug` is a separate boolean, not counted into verbosity."""
    flags, _ = parse_global_flags(["--debug", "summary"])
    assert flags.debug is True
    assert flags.verbosity == 0


def test_parse_strips_global_flags_from_remaining():
    """The subcommand argv must be free of global flags."""
    _, rest = parse_global_flags(["-v", "--debug", "--json", "--host=h", "summary", "--name=Pump"])
    assert rest == ["summary", "--name=Pump"]


def test_promote_help_inserts_separator():
    """`<cmd> --help` -> `<cmd> -- --help` so Fire renders per-command help."""
    assert _promote_help(["summary", "--help"]) == ["summary", "--", "--help"]
    assert _promote_help(["agent", "run", "--help"]) == ["agent", "run", "--", "--help"]
    assert _promote_help(["-h"]) == ["--", "--help"]


def test_promote_help_passthrough_when_absent():
    """Without --help in argv, _promote_help is a no-op."""
    argv = ["buildings", "--name=Pump"]
    assert _promote_help(argv) == argv


def test_promote_help_does_not_double_insert():
    """If `--` already precedes `--help`, don't add another one."""
    assert _promote_help(["summary", "--", "--help"]) == ["summary", "--", "--help"]
