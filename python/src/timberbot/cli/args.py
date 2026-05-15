"""Argv parsing helpers for the `tbot` CLI."""
from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def cast_value(a: str) -> bool | int | float | str:
    """Convert a CLI string into bool/int/float/str."""
    if a.lower() == "true":
        return True
    if a.lower() == "false":
        return False
    try:
        return int(a)
    except ValueError:
        try:
            return float(a)
        except ValueError:
            return a


@dataclass(frozen=True)
class GlobalFlags:
    json_mode: bool
    help_mode: bool
    host: str | None
    port: int | None
    documents_dir: str | None
    mod_dir: str | None
    auth_token: str | None
    positional: list[str]


_VALUE_PREFIXES = (
    "--host=",
    "--port=",
    "--documents-dir=",
    "--mod-dir=",
    "--auth-token=",
)


def parse_flags(argv: list[str]) -> GlobalFlags:
    """Pull out global flags. Returns the rest as positional.

    Recognised flags: --json, --help/-h, --host=, --port=, --documents-dir=,
    --mod-dir=, --auth-token=.
    """
    help_mode = "--help" in argv or "-h" in argv
    json_mode = "--json" in argv
    host: str | None = None
    port: int | None = None
    documents_dir: str | None = None
    mod_dir: str | None = None
    auth_token: str | None = None
    for a in argv:
        if a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a.startswith("--port="):
            with contextlib.suppress(ValueError):
                port = int(a.split("=", 1)[1])
        elif a.startswith("--documents-dir="):
            documents_dir = a.split("=", 1)[1]
        elif a.startswith("--mod-dir="):
            mod_dir = a.split("=", 1)[1]
        elif a.startswith("--auth-token="):
            auth_token = a.split("=", 1)[1]
    skip = {"--", "--json", "--help", "-h"}
    positional = [
        a for a in argv
        if a not in skip and not any(a.startswith(p) for p in _VALUE_PREFIXES)
    ]
    return GlobalFlags(
        json_mode=json_mode,
        help_mode=help_mode,
        host=host,
        port=port,
        documents_dir=documents_dir,
        mod_dir=mod_dir,
        auth_token=auth_token,
        positional=positional,
    )


def parse_kv_args(args: list[str], valid_params: list[str], on_error: Callable[[str], None]) -> dict[str, Any]:
    """Parse key:value pairs against a known parameter set; calls on_error and exits on failure."""
    kwargs: dict[str, Any] = {}
    for a in args:
        if ":" not in a:
            on_error(f"expected key:value, got '{a}'")
            return kwargs
        key, val = a.split(":", 1)
        kwargs[key] = cast_value(val)
    bad = [k for k in kwargs if k not in valid_params]
    if bad:
        plural = "s" if len(bad) > 1 else ""
        on_error(
            f"unknown parameter{plural} {', '.join(bad)}; "
            f"valid: {', '.join(valid_params) if valid_params else '(none)'}"
        )
    return kwargs


def method_params(method: Any) -> list[str]:
    """Parameter names of a bound method, excluding `self`."""
    sig = inspect.signature(method)
    return [p.name for p in sig.parameters.values() if p.name != "self"]


def format_usage(name: str, method: Any) -> str:
    """One-line usage hint for a method, e.g. `place_building prefab:VALUE x:VALUE [orientation:south]`."""
    parts: list[str] = []
    sig = inspect.signature(method)
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        if p.default is inspect.Parameter.empty:
            parts.append(f"{p.name}:VALUE")
        else:
            parts.append(f"[{p.name}:{p.default}]")
    return f"  {name} {' '.join(parts)}"
