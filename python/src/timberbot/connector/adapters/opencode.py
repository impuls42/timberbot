from __future__ import annotations


class OpencodeAdapter:
    def build_argv(self, binary: str, model: str) -> list[str]:
        return [binary, "acp", "--model", model]
