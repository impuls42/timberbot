"""Claude CLI backend. Argv layout: `claude --system-prompt-file <f> [--model M] [--effort E] <goal>`."""
from __future__ import annotations

from timberbot.agent.backend import AgentContext, _BackendBase, register_backend


@register_backend
class ClaudeBackend(_BackendBase):
    name = "claude"
    binary = "claude"

    def build_argv(self, ctx: AgentContext) -> list[str]:
        argv = [self.binary, "--system-prompt-file", str(ctx.instructions_file)]
        if ctx.model:
            argv += ["--model", ctx.model]
        if ctx.effort:
            argv += ["--effort", ctx.effort]
        argv.append(ctx.goal)
        return argv
