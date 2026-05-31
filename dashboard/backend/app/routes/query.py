"""POST /api/query — start a query, return session id + SSE URL."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth import Caller, resolve_caller
from app.budget import reserve_for_query
from app.cache import try_replay
from app.runner.executor import replay_cached, run_query
from app.runner.intent_adapter import _stub_classify
from app.runner.session import Session
from v2.observability import estimate_cost

router = APIRouter()


class QueryRequest(BaseModel):
    text: str


class QueryResponse(BaseModel):
    session_id: str
    sse_url: str
    intent: str
    args: dict[str, Any]
    estimate_usd: float
    budget_remaining_usd: float | None = None
    rate_remaining: int | None = None
    cached: bool = False
    cached_from: str | None = None
    cached_at_ms: int | None = None


@router.post("/query", response_model=QueryResponse)
async def post_query(
    payload: QueryRequest,
    request: Request,
    caller: Caller = Depends(resolve_caller),
) -> QueryResponse:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="text too long")

    store = request.app.state.store
    manager = request.app.state.session_manager

    # Cheap keyword pre-classify for budget reservation and the guest
    # whitelist gate. We deliberately use the keyword stub (zero cost)
    # here; the real LLM-backed classifier runs inside the trace context
    # in run_query so its tokens show up in the dashboard.
    intent_name, intent_args = _stub_classify(text)

    # Guest may run only whitelisted intents. Reject early so we don't
    # debit budget for something we'll refuse anyway.
    if not caller.may_use_intent(intent_name):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "intent_not_allowed_for_guest",
                "message": f"intent '{intent_name}' 仅限 owner，访客可用查询见 /api/help",
                "intent": intent_name,
            },
        )

    # Cache lookup (guest only). Hit → schedule replay, skip budget.
    cached = await try_replay(store, caller, intent_name, intent_args, text)
    if cached is not None:
        session = Session(session_id=cached["session_id"])
        await manager.register(session)
        cached_full = await store.get_session(cached["cached_from"])
        if cached_full is None:
            cached = None
        else:
            asyncio.create_task(
                replay_cached(
                    manager=manager, session=session, cached_session=cached_full
                )
            )
            return QueryResponse(
                session_id=session.session_id,
                sse_url=f"/sse/trace/{session.session_id}",
                intent=intent_name,
                args=intent_args,
                estimate_usd=0.0,
                budget_remaining_usd=None,
                rate_remaining=None,
                cached=True,
                cached_from=cached["cached_from"],
                cached_at_ms=cached["cached_at_ms"],
            )

    # Live path: reserve budget, register session, kick off background run.
    estimate = estimate_cost(intent_name)
    reservation = await reserve_for_query(store, caller, estimate)

    session_id = f"sess_{uuid.uuid4().hex[:16]}"
    session = Session(session_id=session_id)
    await manager.register(session)
    await store.create_session(
        session_id=session_id, text=text, client_kind=caller.kind, ip=caller.ip
    )

    asyncio.create_task(
        run_query(
            store=store,
            manager=manager,
            session=session,
            caller=caller,
            text=text,
            reserved_estimate_usd=estimate,
        )
    )

    return QueryResponse(
        session_id=session_id,
        sse_url=f"/sse/trace/{session_id}",
        intent=intent_name,
        args=intent_args,
        estimate_usd=reservation["estimate_usd"],
        budget_remaining_usd=reservation["budget_remaining_usd"],
        rate_remaining=reservation["rate_remaining"],
        cached=False,
    )
