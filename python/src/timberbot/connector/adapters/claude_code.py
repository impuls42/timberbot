from __future__ import annotations


class ClaudeCodeAdapter:
    def build_argv(self, binary: str, model: str) -> list[str]:
        # `claude-agent-acp` speaks ACP over stdio with no flags (Claude Code
        # 2.1.x dropped the old `--acp`/`--model` surface). The model is pinned
        # after the handshake via `session/set_model`, so `model` is ignored here.
        return [binary]
