"""Agent orchestration: pluggable backends, prompt assets, runner.

`tbot agent run --goal "..." --backend X` is the single entry point. Backends
(claude, codex, opencode, custom) implement a small `AgentBackend` protocol;
adding a new one is a single file + decorator. The runner gathers colony state
via the HTTP client, merges it with the static system prompt from
`tbot.agent_prompts`, writes a fresh `agent-instructions.md` to the user config
dir, then invokes the backend.
"""
from tbot.agent.backend import AgentBackend, AgentContext, register_backend
from tbot.agent.prompts import (
    build_merged_instructions,
    list_packaged_prompts,
    load_prompt,
)
from tbot.agent.runner import resolve_backend, run_agent

__all__ = [
    "AgentBackend",
    "AgentContext",
    "register_backend",
    "resolve_backend",
    "run_agent",
    "load_prompt",
    "list_packaged_prompts",
    "build_merged_instructions",
]
