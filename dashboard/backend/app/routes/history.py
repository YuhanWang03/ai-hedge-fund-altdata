"""Read-only views over past sessions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import Caller, resolve_caller

router = APIRouter()


@router.get("/sessions")
async def list_sessions(
    request: Request,
    caller: Caller = Depends(resolve_caller),
    limit: int = 50,
) -> dict:
    if limit > 200:
        limit = 200
    store = request.app.state.store
    rows = await store.list_sessions(
        client_kind=caller.kind, ip=caller.ip, limit=limit
    )
    return {"sessions": rows}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    caller: Caller = Depends(resolve_caller),
) -> dict:
    store = request.app.state.store
    row = await store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Guests can only fetch sessions they own (matched by IP).
    if caller.kind == "guest" and row.get("ip") != caller.ip:
        raise HTTPException(status_code=403, detail="forbidden")
    return row
