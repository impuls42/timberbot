"""Custom-template backend.

The user supplies a shell-style template via `--command "..."` with these
substitutions (matches the legacy `BuildCustomCommand` placeholders):

  {skill}            -> instructions_file (alias for {instructions_file})
  {instructions_file}
  {prompt}           -> the goal string
  {prompt_file}      -> instructions_file (alias)
  {model}            -> model (or "")
  {effort}           -> effort (or "")

The expanded string is split via `shlex.split` so quoting works naturally.
"""
from __future__ import annotations

import shlex

from timberbot.agent.backend import AgentContext, _BackendBase, register_backend


@register_backend
class CustomBackend(_BackendBase):
    name = "custom"
    binary = ""  # filled from the template

    def __init__(self, *, template: str, binary_override: str | None = None) -> None:
        super().__init__(binary_override=binary_override)
        if not template:
            raise ValueError("CustomBackend requires a non-empty template")
        self.template = template

    def build_argv(self, ctx: AgentContext) -> list[str]:
        expanded = self.template.format(
            skill=str(ctx.instructions_file),
            instructions_file=str(ctx.instructions_file),
            prompt=ctx.goal,
            prompt_file=str(ctx.instructions_file),
            model=ctx.model or "",
            effort=ctx.effort or "",
        )
        return shlex.split(expanded)
