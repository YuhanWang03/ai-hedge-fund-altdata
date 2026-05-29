"""Budget and rate-limit enforcement for guest callers.

Reserve/settle two-phase:
- reserve_for_query(): atomically adds the intent's estimated cost to
  today's spend. Rejects with 402 if that would exceed the daily cap.
- settle_query(): adds (actual - estimate) once the query completes.
  Negative deltas refund the unspent portion.

Owner queries skip both phases.
"""

from __future__ import annotations

import datetime as dt

from fastapi import HTTPException

from app.auth import Caller
from app.config import SETTINGS
from app.db.store import Store


def _today_bucket() -> int:
    today = dt.datetime.now(dt.timezone.utc).date()
    return today.year * 10000 + today.month * 100 + today.day


async def reserve_for_query(
    store: Store, caller: Caller, estimate_usd: float
) -> dict:
    """Reserve budget + check rate limit. Raises HTTPException on rejection.

    Returns metadata: estimate, daily_used_after_reserve, remaining_quota.
    """
    if caller.is_owner:
        return {
            "estimate_usd": estimate_usd,
            "budget_remaining_usd": None,
            "rate_remaining": None,
        }

    # 1. Rate limit (per IP, hourly). Independent of budget so even cheap
    #    intents can't be hammered.
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    allowed, rate_remaining = await store.rate_limit_check_and_increment(
        caller.ip, now_ts, SETTINGS.per_ip_hourly_limit
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": f"每 IP 每小时上限 {SETTINGS.per_ip_hourly_limit} 次查询",
                "retry_after_s": 3600 - (now_ts % 3600),
            },
        )

    # 2. Daily budget reservation.
    bucket = _today_bucket()
    ok, _spent = await store.budget_reserve(
        bucket, estimate_usd, SETTINGS.daily_budget_usd
    )
    if not ok:
        # Refund the rate-limit increment so this rejection doesn't burn a slot.
        # (Simple approach: don't decrement; the cap will reset in <60min.)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "daily_budget_exhausted",
                "message": (
                    f"今日访客查询预算 ${SETTINGS.daily_budget_usd:.2f} 已用完，"
                    "请明日 UTC 0:00 后再试。"
                ),
                "resets_at_utc": _next_utc_midnight_iso(),
            },
        )
    remaining = SETTINGS.daily_budget_usd - _spent
    return {
        "estimate_usd": estimate_usd,
        "budget_remaining_usd": round(remaining, 4),
        "rate_remaining": rate_remaining,
    }


async def settle_query(
    store: Store, caller: Caller, *, estimate_usd: float, actual_usd: float
) -> None:
    """Reconcile reserved vs actual cost. Owner queries are no-ops."""
    if caller.is_owner:
        return
    delta = actual_usd - estimate_usd
    if abs(delta) < 1e-9:
        return
    await store.budget_settle(_today_bucket(), delta)


async def refund_query(store: Store, caller: Caller, estimate_usd: float) -> None:
    """Refund the full reservation when a query fails before any cost is spent."""
    if caller.is_owner:
        return
    await store.budget_settle(_today_bucket(), -estimate_usd)


async def status(store: Store, caller: Caller) -> dict:
    bucket = _today_bucket()
    spent = await store.budget_status(bucket)
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    rate_remaining = await store.rate_limit_status(
        caller.ip, now_ts, SETTINGS.per_ip_hourly_limit
    )
    return {
        "kind": caller.kind,
        "global_daily_used_usd": round(spent, 4),
        "global_daily_cap_usd": SETTINGS.daily_budget_usd,
        "global_daily_remaining_usd": round(
            max(0.0, SETTINGS.daily_budget_usd - spent), 4
        ),
        "your_ip_hourly_remaining": (
            None if caller.is_owner else rate_remaining
        ),
        "resets_at_utc": _next_utc_midnight_iso(),
    }


def _next_utc_midnight_iso() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    tomorrow = (now + dt.timedelta(days=1)).date()
    midnight = dt.datetime.combine(tomorrow, dt.time(0, 0), tzinfo=dt.timezone.utc)
    return midnight.isoformat()
