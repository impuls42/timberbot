"""Codex CLI backend.

Codex consumes the instructions file via `-c model_instructions_file=...` and
the reasoning effort via `-c model_reasoning_effort=...`. Matches the legacy
quoting used by `TimberbotAgent.cs:652-658`.
"""
from __future__ import annotations

from timberbot.agent.backend import AgentContext, _BackendBase, register_backend


@register_backend
class CodexBackend(_BackendBase):
    name = "codex"
    binary = "codex"

    def build_argv(self, ctx: AgentContext) -> list[str]:
        argv = [
            self.binary,
            "-c", f'model_instructions_file="{ctx.instructions_file}"',
        ]
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["-c", f'model_reasoning_effort="{ctx.effort}"']
        argv.append(ctx.goal)
        return argv
