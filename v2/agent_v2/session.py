"""Adapter over the proven short-lived session resolver from Agent V1."""

from __future__ import annotations

from v2.agent import session as legacy_session
from v2.agent_v2.models import AgentResult, SessionResolution


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

    def clear(self, session_id: str) -> None:
        self.store.clear(session_id)
