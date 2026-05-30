"""Per-session in-memory bookkeeping.

A Session owns the asyncio.Queue that the SSE handler drains. The Trace
sink writes events into the queue via run_coroutine_threadsafe — that's
how the sync v2 code in a worker thread reaches the async world.

Sessions are short-lived (a single query). Once SSE has drained the final
event, the manager evicts the entry. The persistent record lives in
SQLite (sessions table); the in-memory queue is purely for live streaming.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional


# Sentinel posted to the queue to tell SSE consumers we're done.
END_OF_STREAM = {"type": "__end__"}


@dataclass
class Session:
    session_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2000))
    done: asyncio.Event = field(default_factory=asyncio.Event)
    final_reply: Optional[str] = None
    final_cost_usd: float = 0.0
    error: Optional[str] = None
    events: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    """Process-local registry of live sessions."""

    def __init__(self) -> None:
        self._by_id: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def register(self, session: Session) -> None:
        async with self._lock:
            self._by_id[session.session_id] = session

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            return self._by_id.get(session_id)

    async def discard(self, session_id: str) -> None:
        async with self._lock:
            self._by_id.pop(session_id, None)
