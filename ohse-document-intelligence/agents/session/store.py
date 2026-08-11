"""In-memory session-scoped store with TTL and turn limits."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionState:
    session_id: str
    turn_id: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_accessed: float = field(default_factory=time.monotonic)
    chemical_name: str | None = None
    cas: str | None = None
    oel_type: str | None = None
    topic: str | None = None
    formula_id: str | None = None
    formula_variables: dict[str, float] | None = None
    exposure_value: float | None = None
    exposure_unit: str | None = None
    previous_intent: str | None = None
    previous_resolved_query: str | None = None
    previous_query_id: str | None = None
    agent_results: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_gate_context(self) -> dict[str, Any]:
        return {
            "chemical_name": self.chemical_name,
            "cas": self.cas,
            "formula_id": self.formula_id,
            "previous_intent": self.previous_intent,
            "turn_id": self.turn_id,
        }


class SessionStore:
    """Thread-safe in-memory session store with TTL and max-turn limits."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_turns: int = 50,
        max_sessions: int = 10000,
    ) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.max_sessions = max_sessions

    def create_session(self, session_id: str | None = None) -> SessionState:
        sid = session_id or str(uuid.uuid4())
        with self._lock:
            self._evict_expired()
            if sid not in self._sessions:
                if len(self._sessions) >= self.max_sessions:
                    self._evict_oldest()
                self._sessions[sid] = SessionState(session_id=sid)
            state = self._sessions[sid]
            state.last_accessed = time.monotonic()
            return state

    def get(self, session_id: str) -> SessionState | None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return None
            if self._is_expired(state):
                del self._sessions[session_id]
                return None
            state.last_accessed = time.monotonic()
            return state

    def save(self, state: SessionState) -> None:
        with self._lock:
            if state.turn_id >= self.max_turns:
                # Session exceeded max turns — reset but keep ID
                state.history = state.history[-5:]
            state.last_accessed = time.monotonic()
            self._sessions[state.session_id] = state

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _is_expired(self, state: SessionState) -> bool:
        return (time.monotonic() - state.last_accessed) > self.ttl_seconds

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, s in self._sessions.items() if (now - s.last_accessed) > self.ttl_seconds]
        for sid in expired:
            del self._sessions[sid]

    def _evict_oldest(self) -> None:
        if not self._sessions:
            return
        oldest = min(self._sessions, key=lambda sid: self._sessions[sid].last_accessed)
        del self._sessions[oldest]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "ttl_seconds": self.ttl_seconds,
                "max_turns": self.max_turns,
            }
