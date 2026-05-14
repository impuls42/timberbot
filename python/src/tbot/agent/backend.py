"""Agent backend protocol + registry.

A backend turns an `AgentContext` (goal, model, effort, instructions file,
working dir, terminal prefix) into an argv list that, when executed, drops
the player into a live AI session.

Backends declare `name` and `binary` and implement `build_argv`. The default
`run` method spawns via `subprocess.run` and returns the exit code; backends
that need fancier orchestration (e.g. terminal wrapping on Windows) can
override it.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentContext:
    """Inputs to a backend invocation."""

    goal: str
    instructions_file: Path
    cwd: Path
    model: str | None = None
    effort: str | None = None
    binary_override: str | None = None
    terminal_prefix: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class AgentBackend(Protocol):
    """Pluggable agent CLI dispatcher."""

    name: str
    binary: str

    def build_argv(self, ctx: AgentContext) -> list[str]:
        """Return the argv list (binary plus its args) for this backend."""
        ...

    def run(self, ctx: AgentContext) -> int:
        """Spawn the agent and return its exit code."""
        ...


def default_run(backend: AgentBackend, ctx: AgentContext) -> int:
    """Standard `run` implementation: build argv, optionally wrap in a terminal, spawn, wait.

    `terminal_prefix` accepts a `{cwd}` placeholder (matches the legacy C# setting
    `"wt -d {cwd} --"`). When set, the agent argv is appended to the prefix and
    the whole thing is shell-split.
    """
    argv = backend.build_argv(ctx)
    if ctx.terminal_prefix:
        prefix = ctx.terminal_prefix.format(cwd=str(ctx.cwd))
        argv = shlex.split(prefix) + argv

    env = None
    if ctx.extra_env:
        import os
        env = {**os.environ, **ctx.extra_env}

    completed = subprocess.run(argv, cwd=str(ctx.cwd), env=env, check=False)
    return completed.returncode


class _BackendBase:
    """Convenience base class. Concrete backends subclass and set name/binary,
    override `build_argv`. Saves boilerplate; not required by the Protocol.
    """

    name: str = ""
    binary: str = ""

    def __init__(self, binary_override: str | None = None) -> None:
        if binary_override:
            self.binary = binary_override

    def build_argv(self, ctx: AgentContext) -> list[str]:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, ctx: AgentContext) -> int:
        return default_run(self, ctx)


# Backend registry. Modules under `tbot.agent.backends` call `register_backend`
# at import time. The runner imports the backends package once to populate.
_REGISTRY: dict[str, type[_BackendBase]] = {}


def register_backend(cls: type[_BackendBase]) -> type[_BackendBase]:
    """Decorator: add a backend class to the registry, keyed by `cls.name`."""
    if not cls.name:
        raise ValueError(f"backend class {cls.__name__} has empty `name`")
    _REGISTRY[cls.name] = cls
    return cls


def get_backend(name: str, **kwargs: object) -> AgentBackend:
    """Instantiate a backend by name. Raises `KeyError` if unknown."""
    cls = _REGISTRY[name]
    return cls(**kwargs)  # type: ignore[arg-type,return-value]


def known_backend_names() -> list[str]:
    """Sorted list of registered backend names."""
    return sorted(_REGISTRY)


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr)
