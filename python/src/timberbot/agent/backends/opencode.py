"""opencode CLI backend. Argv: `opencode run --prompt-file <f> [--model M] <goal>`.

The opencode CLI has no `--effort` flag, so `ctx.effort` is silently dropped
even if the caller supplies one. Use the `claude` or `codex` backends if you
need reasoning-effort control.
"""
from __future__ import annotations

from timberbot.agent.backend import AgentContext, _BackendBase, register_backend


@register_backend
class OpencodeBackend(_BackendBase):
    name = "opencode"
    binary = "opencode"

    def build_argv(self, ctx: AgentContext) -> list[str]:
        argv = [self.binary, "run", "--prompt-file", str(ctx.instructions_file)]
        if ctx.model:
            argv += ["--model", ctx.model]
        # ctx.effort is intentionally ignored - opencode CLI has no effort knob.
        argv.append(ctx.goal)
        return argv
