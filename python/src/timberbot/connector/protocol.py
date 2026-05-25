from __future__ import annotations

# The Agent Client Protocol wire format is owned by the `agent-client-protocol`
# SDK (`import acp`); this module only carries Timberbot-specific extension
# constants that live outside the spec.

# Timberbot extension method. The old `claude --acp` forwarded MCP
# server-initiated elicitations under this method. Standard ACP has no
# elicitation primitive and `claude-agent-acp` does not re-emit it, but the
# connector keeps a handler wired (via the SDK's `ext_notification`) so the
# surface can be restored later without a protocol change. See issue tracker:
# "Restore game elicitation under claude-agent-acp".
NOTIF_GAME_ELICITATION = "game/elicitation"
