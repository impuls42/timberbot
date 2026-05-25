from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from timberbot.connector.agent_spec import TIMBERBOT_SPEC, render_bootstrap_prompt
from timberbot.user_api.protocol import (
    AgentFeedback,
    GameElicitation,
    SessionStateChange,
    SubagentStatusChange,
    TextChunk,
    ToolAction,
    UserAdapter,
)
from timberbot.user_api.session_manager import SessionManager

log = logging.getLogger("timberbot.user_api")


class ModUnreachableError(RuntimeError):
    """Raised when `tbot serve` can't reach the mod at startup.

    Distinct from a transient mid-session disconnect — those are handled
    silently by `TimberbotWsClient.messages()`'s reconnect loop. This one
    means the player almost certainly hasn't launched Timberborn with the
    mod loaded yet, and the CLI surface should print an actionable message
    instead of a 100-line ExceptionGroup traceback.
    """


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
    acp_binary: str = "claude-agent-acp"
    allowed_tools: list[str] = field(default_factory=lambda: ["game.*"])
    telegram_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    # When True (default), the startup ping probe retries forever with
    # exp_backoff until the mod responds. Lets `tbot serve` be launched
    # before the game so the player can start them in either order.
    # When False, the probe fails fast with `ModUnreachableError` — kept
    # for scripts and CI flows that want a clean exit if the mod is down.
    wait_for_mod: bool = True
    # Phase 2 — bounds the per-turn `delegate(...)` / `subagent_reply(...)`
    # call before it transitions the run to `errored` with
    # `last_error="timeout after Ns"`. 60s matches what the main agent's
    # own turns settle in for typical game-state queries.
    subagent_call_timeout_s: float = 60.0
    # Phase 2 — how long an idle / completed / errored / cancelled run
    # stays in the registry before the background sweeper closes it.
    # 600s = 10 min, the §6.1 design-doc default.
    subagent_idle_timeout_s: float = 600.0


def _bind_callbacks(session: object, user_adapter: UserAdapter, user_id: str) -> None:
    """Forward `session/update` and `game/elicitation` notifications to the user adapter.

    `user_id` is captured into the callbacks so the adapter can route a chunk
    back to the right chat even if the session_id binding hasn't reached the
    adapter yet (race between `register_chat` and the first agent reply).
    """

    async def _on_update(sid: str, chunk: str) -> None:
        await user_adapter.send(TextChunk(session_id=sid, text=chunk, user_id=user_id))

    async def _on_elicitation(sid: str, params: dict) -> None:
        await user_adapter.send(GameElicitation(
            session_id=sid,
            question=str(params.get("question", "")),
            choices=list(params.get("choices", [])),
            correlation_id=str(params.get("correlationId", "")),
            user_id=user_id,
        ))

    async def _on_tool_action(sid: str, summary: str, ok: bool) -> None:
        await user_adapter.send(ToolAction(
            session_id=sid, summary=summary, ok=ok, user_id=user_id,
        ))

    session.on_update = _on_update           # type: ignore[attr-defined]
    session.on_elicitation = _on_elicitation  # type: ignore[attr-defined]
    session.on_tool_action = _on_tool_action  # type: ignore[attr-defined]


def _make_subagent_session_binder(user_adapter: UserAdapter, user_id: str):
    """Return a callback the registry invokes for every new subagent session.

    Wires `on_tool_action` so the subagent's write-tool calls surface in
    Telegram with a `[<subagent_id>] …` prefix. Streaming text (`on_update`)
    is intentionally NOT routed — the design surfaces only status flips and
    tool actions for subagents, keeping the chat from drowning in fan-out
    chatter.
    """

    def _bind(run, session):  # type: ignore[no-untyped-def]
        sid = run.subagent_id

        async def _on_tool_action(_acp_sid: str, summary: str, ok: bool) -> None:
            await user_adapter.send(ToolAction(
                session_id=session.session_id,
                summary=summary,
                ok=ok,
                user_id=user_id,
                subagent_id=sid,
            ))

        session.on_tool_action = _on_tool_action

    return _bind


def _make_status_observer(user_adapter: UserAdapter, user_id: str):
    """Return a status-change observer the registry hands to each new run.

    Fired on every `SubagentRun.status` transition; emits a
    `SubagentStatusChange` so the Telegram adapter can render a single
    concise line for the terminal transitions (the adapter filters the
    noisy `idle → running` flips out itself).
    """

    async def _observe(run, prev_status: str, new_status: str, detail: str | None) -> None:
        await user_adapter.send(SubagentStatusChange(
            user_id=user_id,
            subagent_id=run.subagent_id,
            agent=run.spec.slug,
            prev_status=prev_status,
            new_status=new_status,
            detail=detail,
        ))

    return _observe


def _format_state_oneline(summary: object) -> str:
    """One-line game-state digest for session-start previews.

    Defensive against missing fields — `summary` is a pydantic Summary but we
    treat every attribute as optional so a partial mod response (e.g. a save
    still loading) doesn't crash the preview.
    """
    parts: list[str] = []
    t = getattr(summary, "time", None)
    if t is not None and getattr(t, "dayNumber", None) is not None:
        parts.append(f"day {t.dayNumber}")
        speed = getattr(t, "speed", None)
        if speed is not None:
            parts.append(f"{int(getattr(speed, 'value', speed))}x speed")
    districts = getattr(summary, "districts", None) or []
    total_pop = 0
    for d in districts:
        pop = getattr(d, "population", None)
        if pop is not None:
            total_pop += int(getattr(pop, "adults", 0) or 0) + int(getattr(pop, "children", 0) or 0)
    if total_pop:
        parts.append(f"pop {total_pop}")
    alerts = getattr(summary, "alerts", None) or {}
    if alerts:
        n = sum(int(v or 0) for v in alerts.values())
        if n:
            parts.append(f"{n} alert{'s' if n != 1 else ''}")
    weather = getattr(summary, "weather", None)
    if weather is not None and getattr(weather, "isHazardous", False):
        remaining = getattr(weather, "hazardousWeatherDuration", None)
        parts.append(f"hazardous ({remaining}d left)" if remaining else "hazardous")
    return " · ".join(parts) if parts else "no state yet"


def _format_state_full(summary: object) -> str:
    """Multi-line dashboard for `/state` — one section per area."""
    lines: list[str] = []
    settlement = getattr(summary, "settlement", None)
    faction = getattr(summary, "faction", None)
    t = getattr(summary, "time", None)
    header_bits: list[str] = []
    if settlement:
        header_bits.append(str(settlement))
    if faction:
        header_bits.append(f"({faction})")
    if t is not None and getattr(t, "dayNumber", None) is not None:
        progress = getattr(t, "dayProgress", None)
        progress_str = f" {progress:.0%}" if isinstance(progress, (int, float)) else ""
        header_bits.append(f"— day {t.dayNumber}{progress_str}")
        speed = getattr(t, "speed", None)
        if speed is not None:
            header_bits.append(f"@ {int(getattr(speed, 'value', speed))}x")
    if header_bits:
        lines.append(" ".join(header_bits))

    weather = getattr(summary, "weather", None)
    if weather is not None:
        hazard = bool(getattr(weather, "isHazardous", False))
        kind = "hazardous" if hazard else "temperate"
        remaining = (
            getattr(weather, "hazardousWeatherDuration", None) if hazard
            else getattr(weather, "temperateWeatherDuration", None)
        )
        cycle = getattr(weather, "cycle", None)
        cycle_str = f" cycle {cycle}" if cycle is not None else ""
        remaining_str = f", {remaining}d left" if remaining is not None else ""
        lines.append(f"Weather: {kind}{cycle_str}{remaining_str}")

    science = getattr(summary, "science", None)
    if science is not None:
        lines.append(f"Science: {science} pts")

    districts = getattr(summary, "districts", None) or []
    if districts:
        lines.append(f"Districts: {len(districts)}")
        for d in districts:
            name = getattr(d, "name", "?")
            pop = getattr(d, "population", None)
            housing = getattr(d, "housing", None)
            employment = getattr(d, "employment", None)
            wellbeing = getattr(d, "wellbeing", None)
            bits: list[str] = []
            if pop is not None:
                a = int(getattr(pop, "adults", 0) or 0)
                c = int(getattr(pop, "children", 0) or 0)
                b = int(getattr(pop, "bots", 0) or 0)
                bits.append(f"{a + c} pop ({a}+{c}c, {b} bot)")
            if housing is not None:
                occ = getattr(housing, "occupiedBeds", None)
                total = getattr(housing, "totalBeds", None)
                homeless = getattr(housing, "homeless", None) or 0
                if occ is not None and total is not None:
                    h = f" +{homeless} homeless" if homeless else ""
                    bits.append(f"housing {occ}/{total}{h}")
            if employment is not None:
                vac = getattr(employment, "vacancies", None) or 0
                unemp = getattr(employment, "unemployed", None) or 0
                if vac or unemp:
                    bits.append(f"jobs: {vac} vac, {unemp} idle")
            if wellbeing is not None:
                avg = getattr(wellbeing, "average", None)
                crit = getattr(wellbeing, "critical", None) or 0
                if avg is not None:
                    crit_str = f", {crit} critical" if crit else ""
                    bits.append(f"wellbeing {avg:.1f}{crit_str}")
            lines.append(f"  • {name}: " + "; ".join(bits) if bits else f"  • {name}")

    alerts = getattr(summary, "alerts", None) or {}
    if alerts:
        total = sum(int(v or 0) for v in alerts.values())
        if total:
            top = sorted(alerts.items(), key=lambda kv: -int(kv[1] or 0))[:5]
            detail = ", ".join(f"{k}: {v}" for k, v in top)
            lines.append(f"Alerts ({total}): {detail}")

    buildings = getattr(summary, "buildings", None) or {}
    if buildings:
        top = sorted(buildings.items(), key=lambda kv: -int(kv[1] or 0))[:5]
        lines.append("Buildings: " + ", ".join(f"{k}={v}" for k, v in top))

    return "\n".join(lines) if lines else "(no state — mod returned empty summary)"


async def _fetch_summary(client: object) -> object | None:
    """Pull `/api/summary` off the event loop. Returns None on failure."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, client.summary)  # type: ignore[attr-defined]
    except Exception:
        log.exception("failed to fetch /api/summary for state preview")
        return None


def _session_state(session: object) -> str:
    s = getattr(session, "state", None)
    return str(getattr(s, "value", s)) if s is not None else "unknown"


async def _probe_mod_until_reachable(client: object, cfg: ServeConfig) -> None:
    """Block until the mod answers `/api/ping`, or fail fast if `wait_for_mod=False`.

    Single-attempt mode (`wait_for_mod=False`) raises `ModUnreachableError`
    on the first connection failure — the friendly handler in
    `cli.commands.serve` turns it into a one-line CLI error.

    Wait-forever mode (default) loops with `exp_backoff(1s→30s)`, the same
    cadence `tbot watch` / `tbot listen` use. Logging is deliberately
    minimal so it doesn't drown out the rest of the startup line-up: the
    first retry logs `serve: waiting for mod at … Retrying every Ns…` at
    INFO so the operator can see why startup is blocked, subsequent retries
    log at DEBUG to avoid spam, and a final `serve: mod reachable at …`
    line at INFO confirms when the wait clears. The probe runs once per
    iteration in an executor so the blocking `requests` call doesn't stall
    the event loop.
    """
    import requests  # noqa: PLC0415

    from timberbot.utils import exp_backoff  # noqa: PLC0415

    loop = asyncio.get_running_loop()

    def _probe() -> tuple[bool, BaseException | None]:
        try:
            client._get_json("/api/ping")  # type: ignore[attr-defined]  # noqa: SLF001
            return True, None
        except (requests.ConnectionError, requests.Timeout) as e:
            return False, e

    if not cfg.wait_for_mod:
        ok, exc = await loop.run_in_executor(None, _probe)
        if not ok:
            raise ModUnreachableError(
                f"cannot reach mod at http://{cfg.host}:{cfg.port}: "
                f"{type(exc).__name__}. Launch Timberborn with the Timberbot "
                "mod loaded, then try again — or omit --no-wait to let serve "
                "wait until the mod comes up."
            ) from exc
        return

    attempt = 0
    announced = False
    while True:
        ok, exc = await loop.run_in_executor(None, _probe)
        if ok:
            if announced:
                log.info("serve: mod reachable at http://%s:%s", cfg.host, cfg.port)
            return
        delay = exp_backoff(attempt)
        if not announced:
            log.info(
                "serve: waiting for mod at http://%s:%s (launch Timberborn + load a save). "
                "Retrying every %.0fs…", cfg.host, cfg.port, delay,
            )
            announced = True
        else:
            log.debug(
                "serve: mod unreachable (%s); retry in %.1fs", type(exc).__name__, delay,
            )
        await asyncio.sleep(delay)
        attempt += 1


def _prepare_agent_cwd() -> str:
    """Stable, sterile directory passed to ACP `new_session(cwd=…)`.

    ACP requires an absolute cwd. We keep this directory empty so the
    agent runtime can't pick up unrelated project context (no CLAUDE.md,
    no .claude/agents, no .opencode/agent). All identity, tool scope, and
    behavior rules are injected via the prompt — see
    `connector/agent_spec.py:render_bootstrap_prompt`.
    """
    from pathlib import Path  # noqa: PLC0415

    from timberbot.config import config_dir  # noqa: PLC0415

    cwd: Path = config_dir() / "serve"
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)


def _mcp_servers_for_user(cfg: ServeConfig, user_id: str) -> list[dict]:
    """Build the MCP server config the agent connects to.

    `X-Timberbot-User-Id` is added to the SSE request headers so the MCP
    server's delegate-family tools can find the calling user's broker entry
    (see `game_mcp/delegation.py:SubagentBroker.lookup_by_request`).
    """
    return [{
        "type": "sse",
        "name": "game",
        "url": f"http://{cfg.mcp_host}:{cfg.mcp_port}/sse",
        "headers": [
            {"name": "X-Timberbot-User-Id", "value": str(user_id)},
        ],
    }]


async def _user_message_loop(
    user_adapter: UserAdapter,
    session_mgr: SessionManager,
    acp: object,
    cfg: ServeConfig,
    client: object | None = None,
    agent_cwd: str | None = None,
    broker: object | None = None,
) -> None:
    """Drive the inbound message queue until cancelled.

    Shutdown is cancel-only: the TaskGroup in run_serve() cancels this
    coroutine on exit, and the finally block below closes all open
    AgentConnections so their subprocesses are cleaned up.

    `client` is the TimberbotClient used to render game-state previews for
    `/state` and on session-start. Optional so unit tests can keep
    passing a 3-arg call shape; in production `run_serve` always supplies it.

    `broker` is the `SubagentBroker` from `game_mcp.delegation` — when set,
    each newly opened user `AgentConnection` is registered so the
    delegate-family MCP tools can route per-user; on eviction we unregister
    and close any live subagents.
    """
    _connections: dict[str, object] = {}    # user_id -> AgentConnection
    _sessions: dict[str, object] = {}       # user_id -> main Session
    register = getattr(user_adapter, "register_chat", None)

    async def _evict(user_id: str) -> None:
        """Drop a user's main session + connection. Close any subagents."""
        _sessions.pop(user_id, None)
        conn = _connections.pop(user_id, None)
        if conn is not None:
            try:
                await conn.close()  # type: ignore[union-attr]
            except Exception:
                log.exception("error closing connection on eviction for user %s", user_id)
        if broker is not None:
            try:
                await broker.unregister(user_id)  # type: ignore[union-attr]
            except Exception:
                log.exception("error unregistering broker entry for user %s", user_id)

    try:
        async for msg in user_adapter.messages():
            user_id = msg.user_id
            text = msg.text
            log.debug("User %s: %r", user_id, text)

            try:
                # Control commands — route to ACP session lifecycle, not prompt.
                # `/cancel` is the soft cancel: interrupt the in-flight turn
                # (main + every running subagent for that user) without
                # tearing the AgentConnection down. Next user message reuses
                # the same session — main agent retains conversation memory,
                # subagent runs stay reachable by id. `/halt` is the hard
                # form: cancel and evict the whole connection.
                if text in ("/cancel", "/halt"):
                    if user_id in _sessions:
                        session = _sessions[user_id]
                        acp_sid = session.session_id  # type: ignore[union-attr]
                        try:
                            await session.cancel()  # type: ignore[union-attr]
                        except Exception:
                            log.exception("Error sending cancel for user %s", user_id)
                        # Cancel every in-flight subagent turn too — they
                        # share the user's main connection and the user
                        # expects "stop everything" to mean all of it.
                        if broker is not None:
                            state = broker.get(user_id)  # type: ignore[union-attr]
                            if state is not None:
                                for run in state.registry.list():
                                    if (
                                        run.turn_task is not None
                                        and not run.turn_task.done()
                                    ):
                                        try:
                                            await state.registry.cancel(run.subagent_id)
                                        except Exception:
                                            log.exception(
                                                "error cancelling subagent %s",
                                                run.subagent_id,
                                            )
                        await user_adapter.send(SessionStateChange(
                            session_id=acp_sid,
                            state="halting",
                            detail=f"acked {text}",
                            user_id=user_id,
                        ))
                        if text == "/halt":
                            await _evict(user_id)
                        else:
                            # Soft cancel: the session was put into HALTING
                            # by `session.cancel()`. We want subsequent
                            # messages to keep reusing it, so the next-turn
                            # path needs to see it as ACTIVE. Flip back so
                            # the stale-session check doesn't auto-evict.
                            try:
                                from timberbot.connector.session import SessionState  # noqa: PLC0415
                                session.state = SessionState.ACTIVE  # type: ignore[union-attr]
                            except Exception:
                                log.exception("error restoring session state for user %s", user_id)
                    else:
                        # Make the no-op explicit so the user gets a reply
                        # instead of silence.
                        await user_adapter.send(SessionStateChange(
                            session_id="",
                            state="no session",
                            detail=f"nothing to {text.lstrip('/')}",
                            user_id=user_id,
                        ))
                    continue

                if text == "/status":
                    if user_id in _sessions:
                        session = _sessions[user_id]
                        await user_adapter.send(SessionStateChange(
                            session_id=session.session_id,  # type: ignore[union-attr]
                            state=_session_state(session),
                            user_id=user_id,
                        ))
                    else:
                        await user_adapter.send(SessionStateChange(
                            session_id="",
                            state="no session",
                            detail=f"no agent connected yet for user {user_id}",
                            user_id=user_id,
                        ))
                    continue

                if text == "/state":
                    sid = _sessions[user_id].session_id if user_id in _sessions else ""  # type: ignore[union-attr]
                    if client is None:
                        await user_adapter.send(SessionStateChange(
                            session_id=sid,
                            state="info",
                            detail="state unavailable (no game client wired)",
                            user_id=user_id,
                        ))
                        continue
                    summary = await _fetch_summary(client)
                    body = (
                        _format_state_full(summary)
                        if summary is not None
                        else "couldn't reach the mod — is the game running?"
                    )
                    await user_adapter.send(SessionStateChange(
                        session_id=sid,
                        state="info",
                        detail=body,
                        user_id=user_id,
                    ))
                    continue

                # Elicitation answer — rewrite to a prompt the agent can read on its next turn
                if text.startswith("choice:"):
                    parts = text.split(":", 2)
                    if len(parts) == 3:
                        text = f"User selected: {parts[2]} (correlationId={parts[1]})"

                # Evict stale sessions (the agent process died, or a previous /cancel left ENDED state)
                if user_id in _sessions and _session_state(_sessions[user_id]) in ("halting", "ended"):
                    log.info("Evicting stale session for user %s", user_id)
                    await _evict(user_id)

                # First contact (or reconnect after eviction): bring up an ACP session
                session_mgr.get_or_create(user_id)
                is_new_session = user_id not in _sessions
                if is_new_session:
                    mcp_servers = _mcp_servers_for_user(cfg, user_id)
                    conn = await acp.connect(  # type: ignore[union-attr]
                        binary=cfg.acp_binary, model=cfg.model,
                    )
                    session = await conn.new_session(  # type: ignore[union-attr]
                        cwd=agent_cwd or ".",
                        mcp_servers=mcp_servers,
                        allowed_tools=cfg.allowed_tools,
                    )
                    _bind_callbacks(session, user_adapter, user_id)
                    _connections[user_id] = conn
                    _sessions[user_id] = session
                    if broker is not None:
                        broker.register(  # type: ignore[union-attr]
                            user_id=user_id,
                            conn=conn,
                            agent_cwd=agent_cwd or ".",
                            mcp_servers=mcp_servers,
                            call_timeout_s=cfg.subagent_call_timeout_s,
                            idle_timeout_s=cfg.subagent_idle_timeout_s,
                            bind_subagent_callbacks=_make_subagent_session_binder(
                                user_adapter, user_id,
                            ),
                            on_status_change=_make_status_observer(
                                user_adapter, user_id,
                            ),
                        )
                    if register is not None and msg.chat_id is not None:
                        register(session.session_id, msg.chat_id)  # type: ignore[union-attr]
                    # Include a one-line game-state preview so the user
                    # immediately sees the situation the agent is operating in.
                    preview: str | None = None
                    if client is not None:
                        summary = await _fetch_summary(client)
                        if summary is not None:
                            preview = _format_state_oneline(summary)
                    await user_adapter.send(SessionStateChange(
                        session_id=session.session_id,  # type: ignore[union-attr]
                        state="active",
                        detail=preview,
                        user_id=user_id,
                    ))

                session = _sessions[user_id]
                # Close out the previous turn's stream so the next agent
                # chunk creates a fresh chat message instead of continuing
                # to edit a placeholder that's now far above the user's
                # latest reply. No-op if the adapter doesn't expose this.
                reset_stream = getattr(user_adapter, "reset_stream", None)
                if reset_stream is not None:
                    reset_stream(session.session_id)  # type: ignore[union-attr]
                # First turn of a new ACP session: prepend the agent spec
                # so identity, tool scope, and refusal rules ride inside
                # the prompt itself (ACP has no system-prompt field). The
                # agent retains it in session memory for subsequent turns,
                # so we only inject once per session.
                prompt_text = text
                if is_new_session:
                    prompt_text = render_bootstrap_prompt(TIMBERBOT_SPEC) + "\n" + text
                await session.prompt(prompt_text)  # type: ignore[union-attr]
            except Exception as exc:
                log.exception("Error dispatching message for user %s", user_id)
                try:
                    sid = _sessions[user_id].session_id if user_id in _sessions else ""  # type: ignore[union-attr]
                    await user_adapter.send(SessionStateChange(
                        session_id=sid,
                        state="error",
                        detail=str(exc),
                        user_id=user_id,
                    ))
                except Exception:
                    log.exception("Also failed to inform user %s about the error", user_id)
    finally:
        for user_id in list(_connections.keys()):
            await _evict(user_id)


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
    from timberbot.game_mcp.delegation import SubagentBroker  # noqa: PLC0415
    from timberbot.game_mcp.server import create_mcp_server  # noqa: PLC0415
    from timberbot.user_api.telegram.bot import TelegramAdapter  # noqa: PLC0415

    client = TimberbotClient(
        host=cfg.host,
        port=cfg.port,
        auth_token=cfg.auth_token,
        json_mode=True,
    )

    # Startup probe. Without this, an unreachable mod would let the MCP
    # server bind and the Telegram bot connect, then the WS ingestor would
    # silently spin in its reconnect loop forever — the user would see
    # "MCP server started" and assume things are working. We probe `ping`
    # explicitly so the user gets a clear status line.
    #
    # Two modes (controlled by `cfg.wait_for_mod`):
    #   - True  (default): retry forever with exp_backoff. Lets the player
    #     launch `tbot serve` and the game in either order. Matches
    #     `tbot watch` / `tbot listen` UX, which also reconnect on their own.
    #   - False: a single attempt — raise `ModUnreachableError` if it fails.
    #     Kept for scripts and CI that want a clean exit if the mod is down.
    #
    # `client.ping()` returns False on connection error (it's designed for
    # polling), so we do the raw GET to get the actual exception with the
    # actionable error class.
    await _probe_mod_until_reachable(client, cfg)

    bus = EventBus()
    ws_client = TimberbotWsClient(cfg.host, cfg.ws_port, cfg.auth_token)
    ingestor = EventIngestor(ws_client, bus)
    broker = SubagentBroker()

    adapter_cls = ClaudeCodeAdapter if cfg.backend == "claude" else OpencodeAdapter
    acp = ACPConnector(adapter=adapter_cls(), allowed_tools=cfg.allowed_tools)

    user_adapter = TelegramAdapter(cfg.telegram_token, allowed_users=cfg.telegram_allowed_users)

    async def _on_complaint(message: str, category: str, severity: str) -> None:
        await user_adapter.send(AgentFeedback(category=category, severity=severity, message=message))

    mcp = create_mcp_server(client, bus, on_complaint=_on_complaint, broker=broker)
    session_mgr = SessionManager()

    # Sandbox cwd holding our `CLAUDE.md` scoping prompt. Passed as cwd to
    # every ACP `new_session` so the agent loads our role/tool-scope
    # instructions instead of picking up unrelated project context from
    # wherever `tbot serve` was launched. Lives under the user config dir
    # so the path is stable across restarts.
    agent_cwd = _prepare_agent_cwd()
    log.info("serve: agent cwd = %s", agent_cwd)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(ingestor.run(), name="ingestor")
        tg.create_task(
            mcp.run_http_async(
                transport="sse",
                host=cfg.mcp_host,
                port=cfg.mcp_port,
                show_banner=False,
            ),
            name="mcp",
        )
        tg.create_task(user_adapter.start(), name="telegram")
        tg.create_task(
            _user_message_loop(
                user_adapter, session_mgr, acp, cfg, client,
                agent_cwd=str(agent_cwd),
                broker=broker,
            ),
            name="msg-loop",
        )
