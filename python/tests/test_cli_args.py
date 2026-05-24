"""Unit tests for timberbot.cli.args."""
from __future__ import annotations

from timberbot.cli.args import GlobalFlags, cast_value, parse_flags, parse_kv_args


def test_cast_value_handles_bool_int_float_str():
    assert cast_value("true") is True
    assert cast_value("False") is False
    assert cast_value("42") == 42
    assert cast_value("3.14") == 3.14
    assert cast_value("hello") == "hello"


def test_parse_flags_picks_up_json_help_host_port():
    flags = parse_flags(["--json", "--help", "--host=1.2.3.4", "--port=9001", "summary", "x:1"])
    assert isinstance(flags, GlobalFlags)
    assert flags.json_mode is True
    assert flags.help_mode is True
    assert flags.host == "1.2.3.4"
    assert flags.port == 9001
    assert flags.auth_token is None
    assert flags.positional == ["summary", "x:1"]


def test_parse_flags_picks_up_auth_token():
    flags = parse_flags(["--auth-token=s3cret", "summary"])
    assert flags.auth_token == "s3cret"
    # Token value must not leak into positionals.
    assert flags.positional == ["summary"]


def test_parse_flags_auth_token_allows_equals_in_value():
    """Bearer tokens commonly contain '=' (base64 padding). Only split on the first."""
    flags = parse_flags(["--auth-token=abc==", "summary"])
    assert flags.auth_token == "abc=="


def test_parse_flags_defaults():
    flags = parse_flags(["summary"])
    assert flags.json_mode is False
    assert flags.help_mode is False
    assert flags.host is None
    assert flags.port is None
    assert flags.auth_token is None
    assert flags.verbosity == 0
    assert flags.debug is False
    assert flags.positional == ["summary"]


def test_parse_flags_verbose_short_counts():
    """`-v` increments verbosity by 1, repeats stack."""
    assert parse_flags(["summary"]).verbosity == 0
    assert parse_flags(["-v", "summary"]).verbosity == 1
    assert parse_flags(["-v", "-v", "summary"]).verbosity == 2
    assert parse_flags(["-vv", "summary"]).verbosity == 2
    assert parse_flags(["-vvv", "summary"]).verbosity == 3


def test_parse_flags_verbose_long_alias():
    """`--verbose` is the long form of `-v`."""
    assert parse_flags(["--verbose", "summary"]).verbosity == 1
    assert parse_flags(["--verbose", "-v", "summary"]).verbosity == 2


def test_parse_flags_debug():
    """`--debug` is a separate boolean, not counted into verbosity."""
    flags = parse_flags(["--debug", "summary"])
    assert flags.debug is True
    assert flags.verbosity == 0


def test_parse_flags_strips_verbose_and_debug_from_positional():
    """Subcommands must see clean argv — global flags don't leak in."""
    flags = parse_flags(["-v", "--debug", "summary", "x:1"])
    assert flags.positional == ["summary", "x:1"]


def test_parse_flags_verbose_does_not_collide_with_kv_value():
    """`somecmd v:1` must not be parsed as `-v`."""
    flags = parse_flags(["place_building", "prefab:HouseLog"])
    assert flags.verbosity == 0
    assert "prefab:HouseLog" in flags.positional


def test_parse_kv_args_returns_dict_for_valid_input():
    errors: list[str] = []
    out = parse_kv_args(["x:1", "y:2.5", "name:Castle"], ["x", "y", "name"], errors.append)
    assert out == {"x": 1, "y": 2.5, "name": "Castle"}
    assert errors == []


def test_parse_kv_args_reports_unknown_param():
    errors: list[str] = []
    parse_kv_args(["bogus:1"], ["x"], errors.append)
    assert errors and "unknown" in errors[0].lower()


def test_parse_kv_args_reports_malformed_arg():
    errors: list[str] = []
    parse_kv_args(["bogus"], ["x"], errors.append)
    assert errors and "key:value" in errors[0]
