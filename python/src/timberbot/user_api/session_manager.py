from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass
class UserSession:
    session_id: str
    controller_id: str
    state: str = "pending"  # mirrors SessionState from connector


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, UserSession] = {}  # keyed by controller_id
        self._by_session: dict[str, UserSession] = {}  # keyed by session_id

    def get_or_create(self, dialog_id: str) -> UserSession:
        if dialog_id in self._sessions:
            return self._sessions[dialog_id]
        session = UserSession(
            session_id=str(uuid.uuid4()),
            controller_id=dialog_id,
        )
        self._sessions[dialog_id] = session
        self._by_session[session.session_id] = session
        return session

    def get(self, dialog_id: str) -> UserSession | None:
        return self._sessions.get(dialog_id)

    def update_state(self, session_id: str, state: str) -> None:
        session = self._by_session.get(session_id)
        if session is not None:
            session.state = state
