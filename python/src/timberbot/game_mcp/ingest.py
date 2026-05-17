"""EventIngestor: reads EventPush frames from TimberbotWsClient → EventBus.

Runs as a long-lived asyncio task alongside the MCP server. On each "event"
frame it maps the raw EventPush to a typed GameEvent (assigning severity via
the static classification table) and pushes it into the shared EventBus.

The ingestor is transparent to reconnects — TimberbotWsClient.messages() auto-
reconnects internally, so the ingestor loop never exits on transient network
loss.
"""
from __future__ import annotations

import logging

from timberbot.api.wsclient import TimberbotWsClient
from timberbot.game_mcp.bus import EventBus, classify_severity
from timberbot.game_mcp.models import GameEvent

log = logging.getLogger("timberbot.game_mcp.ingest")


class EventIngestor:
    """Bridges the game WebSocket channel to the in-process EventBus."""

    def __init__(self, ws_client: TimberbotWsClient, bus: EventBus) -> None:
        self._ws = ws_client
        self._bus = bus

    async def run(self) -> None:
        """Consume messages from the WebSocket forever. Exits only if ws_client is closed."""
        async for msg in self._ws.messages():
            if msg.type != "event":
                continue
            push = msg.payload  # EventPush
            event = GameEvent(
                seq=0,  # assigned by bus.push()
                type=push.event,
                day=push.day,
                timestamp=push.timestamp,
                severity=classify_severity(push.event),
                payload=push.data or {},
            )
            seq = self._bus.push(event)
            log.debug("ingested event type=%s seq=%d day=%d", event.type, seq, event.day)
