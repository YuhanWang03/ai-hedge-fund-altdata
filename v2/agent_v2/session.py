"""Adapter over the proven short-lived session resolver from Agent V1."""

from __future__ import annotations

import re
import threading
import time

from v2.agent import session as legacy_session
from v2.agent_v2.entities import extract_entities
from v2.agent_v2.models import AgentResult, ExecutionPlan, SessionResolution

#: A follow-up that asks something about *the* stock without naming it: it
#: opens with the question itself.  "什么原因跌这么多" after "哪只跌得最多"
#: is about the one the answer named.
_FOLLOW_UP = re.compile(
    r"^(?:那|所以|然后|但|不过|嗯)?[\s,，、]*"
    r"(?:为什么|为啥|为何|什么原因|原因|怎么回事|怎么会|跌|涨|表现|走势|财报|估值|内部人|新闻|催化|风险"
    r"|还值得|值得|能不能|要不要|后续|接下来|前景|基本面|资金流|机构|供应链|产业链|目标价)",
    re.IGNORECASE,
)
#: Wording that names its own scope; such a question is not about the focus stock.
_OWN_SCOPE = re.compile(r"持仓|仓库|仓位|组合|账户|关注|自选|watchlist|portfolio|我的|宏观|市场|大盘|板块|指数|美联储|利率", re.IGNORECASE)
_CITATION = re.compile(r"\[[A-Za-z0-9_.:~-]+\]")


def focus_entities(answer: str) -> tuple[str, ...]:
    """The stocks an answer's first sentence names; a ranking answer names the winner there."""

    first = re.split(r"[。！？\n]", _CITATION.sub("", answer or ""), maxsplit=1)[0]
    return extract_entities(first)


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
        raw = (text or "").strip()
        # A subject-less follow-up about the stock in focus: prepend it.  The
        # legacy resolver only knows pronouns and a bare "为什么".
        if raw and _FOLLOW_UP.match(raw) and not extract_entities(raw) and not _OWN_SCOPE.search(raw) and not legacy_session._PRONOUN.search(raw):
            focus = self.store.last_ticker(session_id)
            if focus:
                rewritten = f"{focus} {raw}"
                return SessionResolution(text=rewritten, rewritten=True, antecedent=focus, note=f"「{raw}」按上文补全为「{rewritten}」")
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
        # A question with no ticker ("哪只跌得最多") gets its focus from the
        # answer, so the next turn can refer back to the stock it named.
        tickers = result.request.entities or focus_entities(result.answer)
        self.store.record(
            result.request.session_id,
            legacy_session.Turn(
                query=result.request.text,
                tickers=tickers,
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
