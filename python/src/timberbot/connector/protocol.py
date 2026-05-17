from __future__ import annotations

ACP_VERSION = "2026-05"

METHOD_INITIALIZE = "initialize"
METHOD_SESSION_NEW = "session/new"
METHOD_SESSION_PROMPT = "session/prompt"
METHOD_SESSION_CANCEL = "session/cancel"

NOTIF_SESSION_UPDATE = "session/update"
NOTIF_SESSION_REQUEST_PERMISSION = "session/requestPermission"
NOTIF_SESSION_ENDED = "session/ended"
NOTIF_GAME_ELICITATION = "game/elicitation"


def build_request(id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def build_notification(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}
