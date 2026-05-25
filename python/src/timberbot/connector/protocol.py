from __future__ import annotations

# Standard Agent Client Protocol wire constants.
#
# These match the schema published by `zed-industries/agent-client-protocol`
# (`@agentclientprotocol/sdk`), which is what the `claude-agent-acp` bridge and
# `opencode acp` speak. `protocolVersion` is a `uint16` (not a date string).
ACP_VERSION = 1

METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SESSION_PROMPT = "session/prompt"
METHOD_SESSION_CANCEL = "session/cancel"
# `session/set_model` is marked unstable in the spec but is how the
# claude-agent-acp bridge pins a model post-handshake (`{sessionId, modelId}`).
# Agents that don't implement it answer with a JSON-RPC error, which the
# connector treats as a soft failure.
METHOD_SESSION_SET_MODEL = "session/set_model"
# Server-initiated request (client must respond), despite the snake_case name.
METHOD_SESSION_REQUEST_PERMISSION = "session/request_permission"

NOTIF_SESSION_UPDATE = "session/update"
# Timberbot-specific extension. The old `claude --acp` forwarded MCP
# server-initiated elicitations under this method; the standard ACP bridge does
# not emit it, but the handler is kept so the surface can be restored later
# without a protocol change. See issue tracker: "Restore game elicitation under
# claude-agent-acp".
NOTIF_GAME_ELICITATION = "game/elicitation"


def build_request(id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def build_notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}
