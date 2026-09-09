"""Adapter over the proven short-lived session resolver from Agent V1."""

from __future__ import annotations

import threading
import time

from v2.agent import session as legacy_session
from v2.agent_v2.models import AgentResult, ExecutionPlan, SessionResolution


class ShortTermSession:
    """Per-session bounded memory; no database writes and no hidden reasoning."""

    def __init__(
        self,
        *,
        ttl_seconds: float = legacy_session.DEFAULT_TTL_SECONDS,
        max_turns: int = legacy_session.DEFAULT_MAX_TURNS,
    ) -> None:
        self.store = legacy_session.SessionStore(
            ttl_seconds=ttl_seconds,
            max_turns=max_turns,
        )
        self.ttl_seconds = float(ttl_seconds)
        self._pending: dict[str, tuple[float, ExecutionPlan]] = {}
        self._lock = threading.Lock()

    def resolve(self, session_id: str, text: str) -> SessionResolution:
        result = self.store.resolve(session_id, text)
        return SessionResolution(
            text=result.text,
            rewritten=result.rewritten,
            antecedent=result.antecedent,
            note=result.note,
        )

    def record(self, result: AgentResult) -> None:
        if not result.request.session_id:
            return
        self.store.record(
            result.request.session_id,
            legacy_session.Turn(
                query=result.request.text,
                tickers=result.request.entities,
                tools_used=tuple(item.capability for item in result.results),
                answer_digest=result.answer[:300],
                path=result.route.kind.value,
            ),
        )

    def set_pending(self, session_id: str, plan: ExecutionPlan) -> None:
        if not session_id:
            return
        with self._lock:
            self._pending[session_id] = (time.monotonic() + self.ttl_seconds, plan)

    def pop_pending(self, session_id: str) -> ExecutionPlan | None:
        with self._lock:
            entry = self._pending.pop(session_id, None)
        if entry is None:
            return None
        expires_at, plan = entry
        return plan if time.monotonic() < expires_at else None

    def clear(self, session_id: str) -> None:
        self.store.clear(session_id)
        with self._lock:
            self._pending.pop(session_id, None)
