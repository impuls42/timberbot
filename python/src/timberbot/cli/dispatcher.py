"""Explicit command registry.

Replaces the implicit `getattr(bot, name)` reflection over the legacy
`Timberbot` class. Built-in subcommands (top, manager, launch, agent) register
explicitly via `CommandRegistry.register`; method-forward commands are still
resolved by `inspect.signature` against `TimberbotClient` in `timberbot.cli.main`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    """A CLI subcommand."""

    name: str
    summary: str
    handler: Callable[[list[str]], int]
    usage: str = ""


class CommandRegistry:
    """Holds the set of CLI subcommands."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        self._commands[command.name] = command

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def items(self) -> list[tuple[str, Command]]:
        return [(n, self._commands[n]) for n in self.names()]


def public_method_names(client_class: type) -> list[str]:
    """Names of public callable methods on the client class (sorted)."""
    out: list[str] = []
    for name in sorted(dir(client_class)):
        if name.startswith("_"):
            continue
        attr = getattr(client_class, name, None)
        if callable(attr):
            out.append(name)
    return out


def doc_first_line(obj: Any) -> str:
    return (getattr(obj, "__doc__", "") or "").split("\n")[0].strip()
