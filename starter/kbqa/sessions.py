"""对话历史。"""

from __future__ import annotations

import threading
from typing import Optional

MAX_TURNS = 6
MAX_SESSIONS = 500


class SessionStore:
    """每个 session_id 各自保留最近几轮，互不串话。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS, max_turns: int = MAX_TURNS) -> None:
        self._sessions: dict[str, list[dict]] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.max_turns = max_turns

    def history(self, session_id: Optional[str]) -> list[dict]:
        if not session_id:
            return []
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def append(self, session_id: Optional[str], turn: dict) -> None:
        if not session_id:
            return
        with self._lock:
            turns = self._sessions.setdefault(session_id, [])
            turns.append(turn)
            del turns[: max(0, len(turns) - self.max_turns)]
            if session_id in self._order:
                self._order.remove(session_id)
            self._order.append(session_id)
            while len(self._order) > self.max_sessions:
                stale = self._order.pop(0)
                self._sessions.pop(stale, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._order.clear()
