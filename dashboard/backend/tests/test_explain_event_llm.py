"""Tests for POST /api/explain_event_llm.

DeepSeek is not actually called — we monkeypatch the _llm_call_async
seam on the route module to inject canned responses and count
invocations. This keeps the test offline and fast while still
exercising the caller-auth + cache logic.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace _llm_call_async with a counting stub. Set the DeepSeek key
    so the endpoint doesn't short-circuit on the missing-key 503.
    """
    from app.routes import explain_event
    from app.config import SETTINGS

    explain_event._cache_clear()

    calls: list[str] = []

    async def stub(prompt: str, *, api_key: str) -> str:
        calls.append(prompt)
        return f"通俗解析 #{len(calls)}"

    monkeypatch.setattr(explain_event, "_llm_call_async", stub)

    # SETTINGS is a frozen dataclass — bypass via object.__setattr__.
    original = SETTINGS.deepseek_api_key
    object.__setattr__(SETTINGS, "deepseek_api_key", "test-key")
    try:
        yield calls
    finally:
        object.__setattr__(SETTINGS, "deepseek_api_key", original)


@pytest.fixture
async def client(tmp_db, fake_llm):
    from app.main import app
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_guest_gets_403(client):
    r = await client.post(
        "/api/explain_event_llm",
        json={"event": {"type": "api_call", "provider": "fd",
                        "fn": "get_prices"}, "intent": "explain_move"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_owner_first_call_hits_llm(client, fake_llm):
    r = await client.post(
        "/api/explain_event_llm",
        headers={"X-Owner-Token": "test-owner-token"},
        json={"event": {"type": "api_call", "provider": "fd",
                        "fn": "get_prices", "ticker": "NVDA"},
              "intent": "explain_move"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False
    assert body["explanation"].startswith("通俗解析")
    assert len(fake_llm) == 1


@pytest.mark.asyncio
async def test_owner_second_call_same_shape_hits_cache(client, fake_llm):
    base = {"event": {"type": "api_call", "provider": "fd",
                      "fn": "get_prices", "ticker": "NVDA"},
            "intent": "explain_move"}
    h = {"X-Owner-Token": "test-owner-token"}
    r1 = await client.post("/api/explain_event_llm", headers=h, json=base)
    assert r1.status_code == 200
    assert r1.json()["cached"] is False

    # Same event shape, different ticker — must hit cache.
    base2 = dict(base)
    base2["event"] = dict(base["event"])
    base2["event"]["ticker"] = "AAPL"
    r2 = await client.post("/api/explain_event_llm", headers=h, json=base2)
    assert r2.status_code == 200
    assert r2.json()["cached"] is True
    assert r2.json()["explanation"] == r1.json()["explanation"]
    # LLM invoked exactly once across both requests.
    assert len(fake_llm) == 1


@pytest.mark.asyncio
async def test_different_event_shapes_get_separate_cache_entries(client, fake_llm):
    h = {"X-Owner-Token": "test-owner-token"}

    # 3 distinct shapes — each should trigger its own LLM call.
    events = [
        {"type": "api_call", "provider": "fd", "fn": "get_prices"},
        {"type": "api_call", "provider": "tavily", "endpoint": "search"},
        {"type": "transform", "op": "etf_diff"},
    ]
    for ev in events:
        r = await client.post(
            "/api/explain_event_llm", headers=h,
            json={"event": ev, "intent": "explain_move"},
        )
        assert r.status_code == 200
        assert r.json()["cached"] is False

    assert len(fake_llm) == 3
    # Re-hit one of them — must come from cache.
    r = await client.post(
        "/api/explain_event_llm", headers=h,
        json={"event": events[0], "intent": "explain_move"},
    )
    assert r.json()["cached"] is True
    assert len(fake_llm) == 3   # no new LLM call


@pytest.mark.asyncio
async def test_no_api_key_returns_503(client, monkeypatch):
    from app.config import SETTINGS
    original = SETTINGS.deepseek_api_key
    object.__setattr__(SETTINGS, "deepseek_api_key", "")
    try:
        r = await client.post(
            "/api/explain_event_llm",
            headers={"X-Owner-Token": "test-owner-token"},
            json={"event": {"type": "api_call", "provider": "fd",
                            "fn": "get_metrics_never_seen_before"},
                  "intent": "summary"},
        )
        assert r.status_code == 503
        assert "DEEPSEEK_API_KEY" in r.json()["detail"]
    finally:
        object.__setattr__(SETTINGS, "deepseek_api_key", original)
