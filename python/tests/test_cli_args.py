"""Unit tests for tbot.cli.args."""
from __future__ import annotations

from tbot.cli.args import GlobalFlags, cast_value, parse_flags, parse_kv_args


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
    assert flags.positional == ["summary", "x:1"]


def test_parse_flags_defaults():
    flags = parse_flags(["summary"])
    assert flags.json_mode is False
    assert flags.help_mode is False
    assert flags.host is None
    assert flags.port is None
    assert flags.positional == ["summary"]


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
