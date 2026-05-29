"""Drive a single query end-to-end on a background asyncio task.

Flow:
    1. Caller has already passed budget/rate-limit checks.
    2. We register a Session (asyncio.Queue + Trace sink) in the manager.
    3. classify intent inline (cheap, ~one LLM call); decide cache eligibility.
    4. Either replay from cache (skip step 5) or run the responder in a
       thread executor, bound to the Trace contextvar.
    5. Persist the final session row, settle the budget.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

from app.auth import Caller
from app.budget import refund_query, settle_query
from app.cache import cache_key, expires_at_ms
from app.config import SETTINGS
from app.db.store import Store
from app.runner.intent_adapter import classify, run_intent
from app.runner.session import END_OF_STREAM, Session, SessionManager
from v2.observability import TRACE_CTX, Trace, estimate_cost

logger = logging.getLogger(__name__)


async def run_query(
    *,
    store: Store,
    manager: SessionManager,
    session: Session,
    caller: Caller,
    text: str,
    reserved_estimate_usd: float,
) -> None:
    """Top-level entry point. Never raises — all failures land as events."""
    loop = asyncio.get_running_loop()
    final_status = "done"
    final_reply: str = ""
    total_cost_usd = 0.0
    intent_name: str | None = None
    intent_args: dict[str, Any] | None = None
    key: str | None = None
    started_ms = int(time.time() * 1000)

    def sink(ev: dict) -> None:
        # Called from worker thread or main loop. Always push via the main
        # loop so the asyncio.Queue isn't touched off-loop.
        session.events.append(ev)
        cost = ev.get("cost_usd")
        if cost:
            nonlocal total_cost_usd
            total_cost_usd += float(cost)
        try:
            loop.call_soon_threadsafe(session.queue.put_nowait, ev)
        except Exception:
            pass

    trace = Trace(session_id=session.session_id, sink=sink)

    # session_start is the first event clients see.
    trace.emit("session_start", text=text, client_kind=caller.kind)

    try:
        intent_name, intent_args = await loop.run_in_executor(
            None, _classify_with_trace, trace, text
        )
        key = cache_key(intent_name, intent_args)

        # Re-check guest whitelist after we know the intent (intent was
        # unknown at /api/query time).
        if not caller.may_use_intent(intent_name):
            trace.emit(
                "error",
                where="auth",
                message=(
                    f"intent '{intent_name}' is owner-only; presenting fallback."
                ),
            )
            final_reply = (
                f"Demo mode: 「{intent_name}」 仅限 owner。"
                "可用查询：异动归因、个股快照、产业链、13F、ETF、最近异动。"
            )
            final_status = "done"
        else:
            final_reply = await loop.run_in_executor(
                None, _run_responder_with_trace, trace, intent_name, intent_args
            )

        trace.emit("chat_message", role="bot", text=final_reply)
        trace.emit(
            "session_end",
            total_cost_usd=round(total_cost_usd, 6),
            elapsed_ms=int(time.time() * 1000) - started_ms,
        )
    except Exception as exc:
        logger.exception("query failed: %s", exc)
        final_status = "error"
        trace.emit("error", where="executor", message=str(exc))
        trace.emit("session_end", total_cost_usd=round(total_cost_usd, 6))
        # Refund the reservation when the failure is pre-LLM (no real spend
        # yet). Settle would have done this via negative delta, but explicit
        # refund makes the intent clear.
        try:
            await refund_query(store, caller, reserved_estimate_usd)
        except Exception:
            logger.exception("refund_query also failed")
    else:
        # Successful completion: reconcile against the reservation.
        try:
            await settle_query(
                store, caller,
                estimate_usd=reserved_estimate_usd,
                actual_usd=total_cost_usd,
            )
        except Exception:
            logger.exception("settle_query failed")
    finally:
        # Persist the session record + queue terminator + discard from manager.
        ttl_ms = (
            expires_at_ms(intent_name)
            if intent_name and final_status == "done"
            else None
        )
        try:
            await store.finalize_session(
                session_id=session.session_id,
                intent=intent_name,
                args=intent_args,
                cache_key=key,
                status=final_status,
                reply_text=final_reply,
                total_cost_usd=total_cost_usd,
                events=session.events,
                expires_at_ms=ttl_ms,
            )
        except Exception:
            logger.exception("finalize_session failed")
        session.final_reply = final_reply
        session.final_cost_usd = total_cost_usd
        await session.queue.put(END_OF_STREAM)
        session.done.set()
        # Leave the entry in the manager briefly so late SSE connects can
        # still drain the queue tail; a separate sweeper could prune later.


def _classify_with_trace(trace: Trace, text: str) -> tuple[str, dict[str, Any]]:
    token = TRACE_CTX.set(trace)
    try:
        return classify(text)
    finally:
        TRACE_CTX.reset(token)


def _run_responder_with_trace(
    trace: Trace, intent: str, args: dict[str, Any]
) -> str:
    token = TRACE_CTX.set(trace)
    try:
        return run_intent(intent, args)
    finally:
        TRACE_CTX.reset(token)


async def replay_cached(
    *, manager: SessionManager, session: Session, cached_session: dict[str, Any]
) -> None:
    """Stream a cached session's events back through the queue with the
    original inter-event timing preserved, so the UI feels live.
    """
    import json

    events = json.loads(cached_session.get("events_json") or "[]")
    if not events:
        await session.queue.put(END_OF_STREAM)
        session.done.set()
        return

    base_ts = events[0].get("ts_ms", 0)
    for ev in events:
        delta_ms = max(0, int(ev.get("ts_ms", base_ts)) - base_ts)
        # Cap replay sleep so a slow original run doesn't bore the user.
        delta_ms = min(delta_ms, 1200)
        await asyncio.sleep(delta_ms / 1000.0)
        # Annotate the event so the UI can tag it visually.
        ev = dict(ev, replayed=True,
                  cached_from=cached_session["session_id"],
                  cached_at_ms=cached_session["created_ms"])
        await session.queue.put(ev)
        base_ts = ev.get("ts_ms", base_ts)
    await session.queue.put(END_OF_STREAM)
    session.done.set()
