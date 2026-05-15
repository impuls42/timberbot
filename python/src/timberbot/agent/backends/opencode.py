"""opencode CLI backend.

Argv shape: `opencode run [--attach URL] [--model M] <goal-with-instructions>`.

The opencode CLI has no `--effort` flag, so `ctx.effort` is silently dropped
even if the caller supplies one. Use the `claude` or `codex` backends if you
need reasoning-effort control.

Prompt delivery: opencode `run` accepts only a positional message argument
(no `--prompt-file` / `--system-prompt-file` analogue, verified against
opencode CLI at implementation time and the docs at
https://opencode.ai/docs/cli/#run-1). We therefore read the merged
instructions file into a string and concatenate it with the goal, passing the
combined text as the positional message. This preserves the file-on-disk
affordance for inspection while keeping the invocation valid.

Attach mode: when `ctx.attach_url` is set, `--attach <url>` is prepended so
the run targets a long-running `opencode serve` instance (typical Steam Deck
workflow: opencode runs as a systemd user service, the user drives it from a
phone). When unset, the argv shape is unchanged from the legacy fresh-process
form.
"""
from __future__ import annotations

from pathlib import Path

from timberbot.agent.backend import AgentContext, _BackendBase, register_backend


@register_backend
class OpencodeBackend(_BackendBase):
    name = "opencode"
    binary = "opencode"

    def build_argv(self, ctx: AgentContext) -> list[str]:
        argv: list[str] = [self.binary, "run"]
        # Empty string is treated as unset so users can clear a config.toml
        # default via `--attach-url ""` (matches the precedence rules in the
        # CLI / runner layer).
        if ctx.attach_url:
            argv += ["--attach", ctx.attach_url]
        if ctx.model:
            argv += ["--model", ctx.model]
        # ctx.effort is intentionally ignored - opencode CLI has no effort knob.

        # Inline-prompt fallback: opencode `run` has no --prompt-file flag, so
        # we splice the system prompt and the goal into a single positional
        # message. The on-disk instructions file still exists under the config
        # dir for human inspection.
        instructions = _read_instructions(ctx.instructions_file)
        if instructions:
            argv.append(f"{instructions}\n\n# GOAL\n\n{ctx.goal}")
        else:
            argv.append(ctx.goal)
        return argv


def _read_instructions(path: Path) -> str:
    """Best-effort read; return empty string if the file is missing/unreadable.

    The runner writes the merged prompt before we get here, so the file is
    expected to exist. In unit tests the file may be absent — in that case we
    silently fall through to a goal-only invocation rather than blowing up.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
