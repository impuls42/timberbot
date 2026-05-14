"""opencode CLI backend. Argv: `opencode run --prompt-file <f> [--model M] <goal>`."""
from __future__ import annotations

from tbot.agent.backend import AgentContext, _BackendBase, register_backend


@register_backend
class OpencodeBackend(_BackendBase):
    name = "opencode"
    binary = "opencode"

    def build_argv(self, ctx: AgentContext) -> list[str]:
        argv = [self.binary, "run", "--prompt-file", str(ctx.instructions_file)]
        if ctx.model:
            argv += ["--model", ctx.model]
        argv.append(ctx.goal)
        return argv
