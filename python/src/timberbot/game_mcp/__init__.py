"""Game MCP server package.

Exposes TimberbotClient methods as MCP tools over HTTP/SSE (fastmcp).
Every tool response embeds an event envelope carrying game events since the
last tool call, so the agent observes world changes without explicit polling.

Typical usage in tbot serve::

    from timberbot.api.client import TimberbotClient
    from timberbot.api.wsclient import TimberbotWsClient
    from timberbot.game_mcp import EventBus, EventIngestor, create_mcp_server

    client = TimberbotClient()
    bus = EventBus()
    ws_client = TimberbotWsClient(host, ws_port)
    ingestor = EventIngestor(ws_client, bus)
    mcp = create_mcp_server(client, bus)

    # Run both concurrently:
    # asyncio.TaskGroup: ingestor.run() + mcp.run_http_async(transport="sse", ...)
"""
from timberbot.game_mcp.bus import EventBus
from timberbot.game_mcp.ingest import EventIngestor
from timberbot.game_mcp.server import create_mcp_server

__all__ = ["EventBus", "EventIngestor", "create_mcp_server"]
