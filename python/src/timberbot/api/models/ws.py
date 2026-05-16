"""Pydantic v2 envelope models for the Timberbot WebSocket protocol.

The WS channel replaces the heartbeat-polling + outbound-HTTP-webhook split
with a single long-lived bidirectional connection. Every frame on the wire is
JSON with a `{type, payload}` envelope — `type` identifies which payload model
to validate against.

These models live in a sibling file (not `_generated.py`) because the WS
protocol is hand-authored and not driven by `openapi.yaml`; the codegen script
fully overwrites `_generated.py` on every run, so anything hand-written there
would be clobbered. They are re-exported from `timberbot.api.models` so the
import path stays stable.
"""
from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from timberbot.api.models._generated import AgentState

T = TypeVar("T")


class WsMessage(BaseModel, Generic[T]):
    """Generic envelope: every WS frame is `{type, payload}`.

    Inbound frames are parsed in two passes: first the envelope itself (to read
    `type`), then `payload` is re-validated against the concrete model for that
    `type`. Outbound frames are constructed with this same shape.
    """

    model_config = ConfigDict(extra="allow")
    type: str
    payload: T


class StatePush(AgentState):
    """Server → client push of the full `AgentState`.

    The payload IS the `AgentState` snapshot — identical wire shape to the
    HTTP `/api/agent/state` response (no extra wrapping). Sent on connect,
    on any state mutation (mode/goal/ready toggle, pending request enqueued),
    and whenever the server wants to nudge the connector to re-evaluate
    dispatch.
    """

    model_config = ConfigDict(extra="allow")


class EventPush(BaseModel):
    """Server → client push of a game event.

    Replaces the outbound-HTTP-webhook channel. `event` is the event name
    (e.g. `drought.start`, `beaver.died`); `data` is event-specific and may
    be `None`.
    """

    model_config = ConfigDict(extra="allow")
    event: str
    day: int = Field(..., ge=0)
    timestamp: int = Field(..., description="Unix-epoch seconds.")
    data: dict | None = None


class HeartbeatRequest(BaseModel):
    """Client → server heartbeat.

    Same fields as the HTTP `/api/tbot/heartbeat` body. Sent every
    `heartbeat_interval` seconds while the socket is open.
    """

    model_config = ConfigDict(extra="allow")
    version: str
    agent_status: str
    acked_request_id: int | None = None


class PingRequest(BaseModel):
    """Either side → either side. Keepalive probe.

    The receiver should respond with a `PongResponse` carrying the same
    `nonce` (when present) so the sender can measure RTT.
    """

    model_config = ConfigDict(extra="allow")
    nonce: str | None = None


class PongResponse(BaseModel):
    """Reply to a `PingRequest`. Echoes the nonce when one was supplied."""

    model_config = ConfigDict(extra="allow")
    nonce: str | None = None


class ErrorPush(BaseModel):
    """Server → client error notification.

    The socket is not necessarily closed after an `ErrorPush` — `code`
    distinguishes fatal (e.g. `unauthorized`) from advisory (e.g.
    `bad_envelope`) cases. Clients should log + surface, not crash.
    """

    model_config = ConfigDict(extra="allow")
    code: str
    message: str


# Convenience: a discriminated lookup table the client uses to pick the right
# payload model after reading `type`. Keys must match the envelope `type` strings
# the server emits (see docs/websocket-protocol.md once Unit 1 lands).
INBOUND_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "state": StatePush,
    "event": EventPush,
    "ping": PingRequest,
    "pong": PongResponse,
    "error": ErrorPush,
}


# Outbound envelope `type` strings the client may send. Defined as a Literal
# alias so callers can get IDE autocompletion without being locked in — the
# protocol is forward-compatible with unknown types.
OutboundType = Literal["heartbeat", "ping", "pong", "ack"]


__all__ = [
    "INBOUND_PAYLOAD_MODELS",
    "ErrorPush",
    "EventPush",
    "HeartbeatRequest",
    "OutboundType",
    "PingRequest",
    "PongResponse",
    "StatePush",
    "WsMessage",
]
