"""End-to-end test of POST /api/query → SSE → cached replay path."""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_db):
    from app.main import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "observability_hooks" in body


@pytest.mark.asyncio
async def test_help_distinguishes_owner_from_guest(client):
    r_guest = await client.get("/api/help")
    assert r_guest.status_code == 200
    assert r_guest.json()["kind"] == "guest"

    r_owner = await client.get(
        "/api/help", headers={"X-Owner-Token": "test-owner-token"}
    )
    assert r_owner.status_code == 200
    assert r_owner.json()["kind"] == "owner"
    assert r_owner.json()["rate_limit"] is None


@pytest.mark.asyncio
async def test_query_rejects_non_whitelisted_intent_for_guest(client):
    # `提醒` triggers alert_set, which is NOT in the guest whitelist.
    r = await client.post("/api/query", json={"text": "提醒我 NVDA 突破 130"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "intent_not_allowed_for_guest"


@pytest.mark.asyncio
async def test_owner_can_run_any_intent(client):
    r = await client.post(
        "/api/query",
        json={"text": "提醒我 NVDA 突破 130"},
        headers={"X-Owner-Token": "test-owner-token"},
    )
    assert r.status_code == 200
    assert r.json()["intent"] == "alert_set"


@pytest.mark.asyncio
async def test_query_then_sse_streams_to_completion(client):
    r = await client.post("/api/query", json={"text": "NVDA 为什么跌？"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "explain_move"
    sid = body["session_id"]

    # Wait for the background task to finish, then pull the persisted events
    # via the SSE endpoint (fallback path).
    await asyncio.sleep(0.5)
    async with client.stream("GET", f"/sse/trace/{sid}") as resp:
        text = b""
        async for chunk in resp.aiter_bytes():
            text += chunk
            if b"stream_close" in text:
                break
    decoded = text.decode("utf-8")
    assert "session_start" in decoded
    assert "session_end" in decoded
    assert "chat_message" in decoded


@pytest.mark.asyncio
async def test_guest_second_identical_query_is_cached(client):
    r1 = await client.post("/api/query", json={"text": "NVDA 为什么跌？"})
    assert r1.status_code == 200
    sid1 = r1.json()["session_id"]
    assert r1.json()["cached"] is False

    # Give the executor a moment to persist the session.
    await asyncio.sleep(1.0)

    r2 = await client.post("/api/query", json={"text": "NVDA 为什么跌？"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["cached"] is True
    assert body["cached_from"] == sid1


@pytest.mark.asyncio
async def test_invalid_owner_token_is_401(client):
    r = await client.post(
        "/api/query",
        json={"text": "NVDA?"},
        headers={"X-Owner-Token": "wrong-token"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_budget_status_for_guest_reflects_spend(client):
    # Run one query so budget moves.
    await client.post("/api/query", json={"text": "summary NVDA"})
    await asyncio.sleep(1.0)

    r = await client.get("/api/budget/status")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "guest"
    assert body["global_daily_cap_usd"] == 0.30
    # Spent some non-negative amount.
    assert body["global_daily_used_usd"] >= 0
