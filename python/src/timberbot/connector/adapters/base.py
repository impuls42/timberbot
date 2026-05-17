from __future__ import annotations

from typing import Protocol


class RuntimeAdapter(Protocol):
    def build_argv(self, binary: str, model: str) -> list[str]:
        ...
