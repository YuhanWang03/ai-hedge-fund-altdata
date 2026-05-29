"""SSE trace stream.

The handler tries the in-memory live queue first; on miss it falls back to
the persisted events_json (covers reconnects after the queue is gone).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.runner.session import END_OF_STREAM

router = APIRouter()
logger = logging.getLogger(__name__)


def _format_sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


async def _stream_live(session, request: Request) -> AsyncIterator[bytes]:
    while True:
        if await request.is_disconnected():
            return
        try:
            ev = await asyncio.wait_for(session.queue.get(), timeout=15.0)
        except asyncio.TimeoutError:
            yield b": keepalive\n\n"
            continue
        if ev is END_OF_STREAM:
            yield _format_sse({"type": "stream_close"})
            return
        yield _format_sse(ev)


async def _stream_persisted(events: list[dict]) -> AsyncIterator[bytes]:
    """Reconnect path: emit every persisted event, then close. No delay
    here — the original time-deltas have already passed for the caller.
    """
    for ev in events:
        yield _format_sse(ev)
    yield _format_sse({"type": "stream_close"})


@router.get("/sse/trace/{session_id}")
async def stream_trace(session_id: str, request: Request) -> StreamingResponse:
    manager = request.app.state.session_manager
    store = request.app.state.store

    session = await manager.get(session_id)
    if session is not None:
        return StreamingResponse(
            _stream_live(session, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    # Fallback: pull from sqlite. Useful for reconnects + sharing links.
    row = await store.get_session(session_id)
    if row is None:
        async def _err() -> AsyncIterator[bytes]:
            yield _format_sse({"type": "error", "message": "session_not_found"})

        return StreamingResponse(_err(), media_type="text/event-stream")

    events = json.loads(row.get("events_json") or "[]")
    return StreamingResponse(
        _stream_persisted(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
