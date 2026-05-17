from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from timberbot.user_api.protocol import (
    GameElicitation,
    SessionStateChange,
    TextChunk,
    UserAdapter,
)
from timberbot.user_api.session_manager import SessionManager

log = logging.getLogger("timberbot.user_api")


@dataclass
class ServeConfig:
    host: str = "127.0.0.1"
    port: int = 8085
    ws_port: int = 8086
    auth_token: str | None = None
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8091
    backend: str = "claude"
    model: str = "claude-opus-4-7"
    acp_binary: str = "claude"
    allowed_tools: list[str] = field(default_factory=lambda: ["game.*"])
    telegram_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)


def _bind_callbacks(handle: object, user_adapter: UserAdapter) -> None:
    """Forward `session/update` and `game/elicitation` notifications to the user adapter."""

    async def _on_update(sid: str, chunk: str) -> None:
        await user_adapter.send(TextChunk(session_id=sid, text=chunk))

    async def _on_elicitation(sid: str, params: dict) -> None:
        await user_adapter.send(GameElicitation(
            session_id=sid,
            question=str(params.get("question", "")),
            choices=list(params.get("choices", [])),
            correlation_id=str(params.get("correlationId", "")),
        ))

    handle.on_update = _on_update           # type: ignore[attr-defined]
    handle.on_elicitation = _on_elicitation  # type: ignore[attr-defined]


def _handle_state(handle: object) -> str:
    s = getattr(handle, "state", None)
    return str(getattr(s, "value", s)) if s is not None else "unknown"


async def _user_message_loop(
    user_adapter: UserAdapter,
    session_mgr: SessionManager,
    acp: object,
    cfg: ServeConfig,
) -> None:
    """Drive the inbound message queue until cancelled.

    Shutdown is cancel-only: the TaskGroup in run_serve() cancels this
    coroutine on exit, and the finally block below closes all open
    SessionHandles so their subprocesses are cleaned up.
    """
    _handles: dict[str, object] = {}       # user_id -> SessionHandle
    _acp_sessions: dict[str, str] = {}     # user_id -> ACP session_id
    register = getattr(user_adapter, "register_chat", None)

    try:
        async for msg in user_adapter.messages():
            user_id = msg.user_id
            text = msg.text
            log.debug("User %s: %r", user_id, text)

            try:
                # Control commands — route to ACP session lifecycle, not prompt
                if text in ("/cancel", "/halt"):
                    if user_id in _handles:
                        handle = _handles.pop(user_id)
                        acp_sid = _acp_sessions.pop(user_id)
                        try:
                            await handle.cancel(acp_sid)  # type: ignore[union-attr]
                        except Exception:
                            log.exception("Error sending cancel for user %s", user_id)
                        await user_adapter.send(SessionStateChange(
                            session_id=acp_sid,
                            state="halting",
                            detail=f"acked {text}",
                        ))
                    continue

                if text == "/status":
                    if user_id in _handles:
                        await user_adapter.send(SessionStateChange(
                            session_id=_acp_sessions[user_id],
                            state=_handle_state(_handles[user_id]),
                        ))
                    else:
                        await user_adapter.send(SessionStateChange(
                            session_id="",
                            state="no session",
                            detail=f"no agent connected yet for user {user_id}",
                        ))
                    continue

                # Elicitation answer — rewrite to a prompt the agent can read on its next turn
                if text.startswith("choice:"):
                    parts = text.split(":", 2)
                    if len(parts) == 3:
                        text = f"User selected: {parts[2]} (correlationId={parts[1]})"

                # Evict stale handles (the agent process died, or a previous /cancel left ENDED state)
                if user_id in _handles and _handle_state(_handles[user_id]) in ("halting", "ended"):
                    log.info("Evicting stale handle for user %s", user_id)
                    _handles.pop(user_id, None)
                    _acp_sessions.pop(user_id, None)

                # First contact (or reconnect after eviction): bring up an ACP session
                session_mgr.get_or_create(user_id)
                if user_id not in _handles:
                    handle = await acp.connect(  # type: ignore[union-attr]
                        binary=cfg.acp_binary, model=cfg.model,
                    )
                    acp_session_id = await handle.new_session(
                        cwd=".",
                        mcp_servers=[{"name": "game", "url": f"http://{cfg.mcp_host}:{cfg.mcp_port}/sse"}],
                    )
                    _bind_callbacks(handle, user_adapter)
                    _handles[user_id] = handle
                    _acp_sessions[user_id] = acp_session_id
                    if register is not None and msg.chat_id is not None:
                        register(acp_session_id, msg.chat_id)
                    await user_adapter.send(SessionStateChange(
                        session_id=acp_session_id, state="active",
                    ))

                handle = _handles[user_id]
                await handle.prompt(_acp_sessions[user_id], text)  # type: ignore[union-attr]
            except Exception as exc:
                log.exception("Error dispatching message for user %s", user_id)
                try:
                    await user_adapter.send(SessionStateChange(
                        session_id=_acp_sessions.get(user_id, ""),
                        state="error",
                        detail=str(exc),
                    ))
                except Exception:
                    log.exception("Also failed to inform user %s about the error", user_id)
    finally:
        for handle in list(_handles.values()):
            try:
                await handle.close()  # type: ignore[union-attr]
            except Exception:
                log.exception("Error closing handle on teardown")


async def run_serve(cfg: ServeConfig) -> None:
    if not cfg.telegram_token:
        raise ValueError(
            "ServeConfig.telegram_token is empty; set [serve.telegram].token in "
            "config.toml, $TBOT_TELEGRAM_TOKEN, or pass --telegram-token."
        )

    from timberbot.api.client import TimberbotClient  # noqa: PLC0415
    from timberbot.api.wsclient import TimberbotWsClient  # noqa: PLC0415
    from timberbot.connector import ACPConnector  # noqa: PLC0415
    from timberbot.connector.adapters.claude_code import ClaudeCodeAdapter  # noqa: PLC0415
    from timberbot.connector.adapters.opencode import OpencodeAdapter  # noqa: PLC0415
    from timberbot.game_mcp import EventBus, EventIngestor  # noqa: PLC0415
    from timberbot.game_mcp.server import create_mcp_server  # noqa: PLC0415
    from timberbot.user_api.telegram.bot import TelegramAdapter  # noqa: PLC0415

    client = TimberbotClient(
        host=cfg.host,
        port=cfg.port,
        auth_token=cfg.auth_token,
        json_mode=True,
    )
    bus = EventBus()
    ws_client = TimberbotWsClient(cfg.host, cfg.ws_port, cfg.auth_token)
    ingestor = EventIngestor(ws_client, bus)
    mcp = create_mcp_server(client, bus)

    adapter_cls = ClaudeCodeAdapter if cfg.backend == "claude" else OpencodeAdapter
    acp = ACPConnector(adapter=adapter_cls(), allowed_tools=cfg.allowed_tools)

    user_adapter = TelegramAdapter(cfg.telegram_token, allowed_users=cfg.telegram_allowed_users)
    session_mgr = SessionManager()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(ingestor.run(), name="ingestor")
        tg.create_task(
            mcp.run_http_async(transport="sse", host=cfg.mcp_host, port=cfg.mcp_port),
            name="mcp",
        )
        tg.create_task(user_adapter.start(), name="telegram")
        tg.create_task(
            _user_message_loop(user_adapter, session_mgr, acp, cfg),
            name="msg-loop",
        )
