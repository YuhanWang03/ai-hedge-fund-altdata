"""Budget reserve/settle + rate-limit tests."""

from __future__ import annotations

import datetime as dt

import pytest

from app.auth import Caller


@pytest.mark.asyncio
async def test_owner_bypasses_everything(store):
    from app.budget import reserve_for_query, settle_query, status

    caller = Caller(kind="owner", ip="1.2.3.4")
    # Reserve 100 calls of $0.10 each — far past the $0.30 cap.
    for _ in range(100):
        meta = await reserve_for_query(store, caller, 0.10)
        assert meta["budget_remaining_usd"] is None  # owner not metered
    await settle_query(store, caller, estimate_usd=0.10, actual_usd=0.08)

    s = await status(store, caller)
    assert s["global_daily_used_usd"] == 0.0
    assert s["your_ip_hourly_remaining"] is None


@pytest.mark.asyncio
async def test_guest_daily_budget_exhausts(store):
    from fastapi import HTTPException

    from app.budget import reserve_for_query

    caller = Caller(kind="guest", ip="9.9.9.9")
    # Cap is $0.30. Each reservation is $0.20. First passes; second
    # would push to $0.40 → reject.
    meta = await reserve_for_query(store, caller, 0.20)
    assert meta["budget_remaining_usd"] == pytest.approx(0.10, abs=1e-6)

    with pytest.raises(HTTPException) as exc:
        await reserve_for_query(store, caller, 0.20)
    assert exc.value.status_code == 402
    assert exc.value.detail["error"] == "daily_budget_exhausted"


@pytest.mark.asyncio
async def test_settle_refunds_unused_estimate(store):
    from app.budget import reserve_for_query, settle_query, status

    caller = Caller(kind="guest", ip="9.9.9.9")
    await reserve_for_query(store, caller, 0.015)
    # Settle to a lower actual cost.
    await settle_query(store, caller, estimate_usd=0.015, actual_usd=0.003)

    s = await status(store, caller)
    assert s["global_daily_used_usd"] == pytest.approx(0.003, abs=1e-6)


@pytest.mark.asyncio
async def test_guest_per_ip_rate_limit(store):
    from fastapi import HTTPException

    from app.budget import reserve_for_query

    caller = Caller(kind="guest", ip="7.7.7.7")
    # 5 allowed per hour.
    for i in range(5):
        meta = await reserve_for_query(store, caller, 0.001)
        assert meta["rate_remaining"] == 4 - i

    with pytest.raises(HTTPException) as exc:
        await reserve_for_query(store, caller, 0.001)
    assert exc.value.status_code == 429
    assert exc.value.detail["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_distinct_ips_dont_share_rate_limit(store):
    from app.budget import reserve_for_query

    a = Caller(kind="guest", ip="1.1.1.1")
    b = Caller(kind="guest", ip="2.2.2.2")

    for _ in range(5):
        await reserve_for_query(store, a, 0.001)
    # b should still have its own 5.
    meta = await reserve_for_query(store, b, 0.001)
    assert meta["rate_remaining"] == 4
