"""Auto-push feed endpoints (owner-only).

Three routes:
- GET /api/recent_pushes      — list recent archive rows for the feed
- GET /api/push_trace/{id}    — full text + parsed trace events for one push
- GET /sse/auto_push          — live stream of new archive rows (polls 1s)

All three are owner-only — the feed shows real production push activity,
which is owner-private by design.

We read v2/data/archive.db directly with aiosqlite. The path is computed
from HEDGE_FUND_REPO_PATH so the same backend can run against a dev
checkout or the production VPS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.auth import Caller, resolve_caller
from app.config import SETTINGS

logger = logging.getLogger(__name__)

# Two routers so we can mount REST under /api and SSE under /sse to
# match the nginx snippet's `/sse/` proxy_buffering=off block.
router = APIRouter()
sse_router = APIRouter()


def _archive_db_path() -> Path:
    return Path(SETTINGS.hedge_fund_repo_path).resolve() / "data" / "archive.db"


async def _open_archive() -> aiosqlite.Connection:
    db = aiosqlite.connect(str(_archive_db_path()))
    conn = await db
    conn.row_factory = aiosqlite.Row
    return conn


def _require_owner(caller: Caller) -> None:
    if caller.kind != "owner":
        raise HTTPException(status_code=403, detail="auto_push feed is owner-only")


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Recent pushes — initial paint
# ---------------------------------------------------------------------------

@router.get("/recent_pushes")
async def recent_pushes(
    caller: Caller = Depends(resolve_caller),
    days: int = 2,
) -> dict:
    """List recent push records from archive.db."""
    _require_owner(caller)

    if not _archive_db_path().exists():
        return {"pushes": [], "warning": "archive.db not found"}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = await _open_archive()
    try:
        async with conn.execute(
            """
            SELECT id, ts, agent, msg_type, title, tickers,
                   substr(text_html, 1, 800) AS preview,
                   (trace_json IS NOT NULL AND length(trace_json) > 2) AS has_trace
            FROM pushes
            WHERE ts >= ?
            ORDER BY id DESC
            LIMIT 200
            """,
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await conn.close()

    return {"pushes": [_row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Single push: full text + trace events
# ---------------------------------------------------------------------------

@router.get("/push_trace/{push_id}")
async def push_trace(
    push_id: int,
    caller: Caller = Depends(resolve_caller),
) -> dict:
    """Return one push's full text_html + parsed trace events."""
    _require_owner(caller)

    if not _archive_db_path().exists():
        raise HTTPException(status_code=404, detail="archive.db not found")

    conn = await _open_archive()
    try:
        async with conn.execute(
            """
            SELECT id, ts, agent, msg_type, title, tickers,
                   text_html, image_path, trace_json
            FROM pushes WHERE id = ?
            """,
            (push_id,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="push not found")

    events: list = []
    raw = row["trace_json"]
    if raw:
        try:
            events = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("malformed trace_json on push %d: %s", push_id, exc)

    return {
        "push": {
            "id": row["id"],
            "ts": row["ts"],
            "agent": row["agent"],
            "msg_type": row["msg_type"],
            "title": row["title"],
            "tickers": row["tickers"],
            "text_html": row["text_html"],
            "image_path": row["image_path"],
        },
        "events": events,
    }


# ---------------------------------------------------------------------------
# SSE: stream new archive rows in real time
# ---------------------------------------------------------------------------

_SSE_POLL_INTERVAL_S = 1.0


@sse_router.get("/sse/auto_push")
async def sse_auto_push(
    request: Request,
    caller: Caller = Depends(resolve_caller),
) -> StreamingResponse:
    """Long-poll SSE: yield a JSON event each time a new archive row appears.

    The implementation is deliberately simple — poll archive.db every
    second for `id > seen_max`. With ~50 pushes/day in production this
    handles bursts (anomaly + 13F) far below saturation.
    """
    _require_owner(caller)

    async def gen() -> AsyncIterator[bytes]:
        if not _archive_db_path().exists():
            yield b'data: {"type":"error","message":"archive.db not found"}\n\n'
            return

        # Seed seen_max from current state so the client only sees NEW rows
        # arriving after the SSE was opened. /api/recent_pushes covered the
        # backlog.
        conn = await _open_archive()
        try:
            async with conn.execute("SELECT MAX(id) AS m FROM pushes") as cur:
                row = await cur.fetchone()
            seen_max = (row["m"] if row and row["m"] is not None else 0)
        finally:
            await conn.close()

        while True:
            if await request.is_disconnected():
                return
            try:
                conn = await _open_archive()
                try:
                    async with conn.execute(
                        """
                        SELECT id, ts, agent, msg_type, title, tickers,
                               substr(text_html, 1, 800) AS preview,
                               (trace_json IS NOT NULL AND length(trace_json) > 2) AS has_trace
                        FROM pushes
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT 50
                        """,
                        (seen_max,),
                    ) as cur:
                        new_rows = await cur.fetchall()
                finally:
                    await conn.close()

                for r in new_rows:
                    payload = _row_to_dict(r)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                    seen_max = max(seen_max, r["id"])
            except Exception as exc:
                logger.warning("sse_auto_push poll failed: %s", exc)

            await asyncio.sleep(_SSE_POLL_INTERVAL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
