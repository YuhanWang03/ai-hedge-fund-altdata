"""Cache key + TTL + replay-session creation."""

from __future__ import annotations

import json
import time

import pytest

from app.auth import Caller
from app.cache import cache_key, expires_at_ms, try_replay, ttl_for_intent
from app.config import SETTINGS


def test_cache_key_is_canonical():
    a = cache_key("explain_move", {"ticker": "NVDA"})
    b = cache_key("explain_move", {"ticker": "NVDA"})
    c = cache_key("explain_move", {"ticker": "AAPL"})
    assert a == b
    assert a != c


def test_cache_key_args_order_independent():
    # JSON sort_keys ensures stable hashing.
    a = cache_key("chain", {"ticker": "AMD", "depth": 2})
    b = cache_key("chain", {"depth": 2, "ticker": "AMD"})
    assert a == b


def test_ttl_per_intent():
    assert ttl_for_intent("explain_move") == 5 * 60
    assert ttl_for_intent("summary") == 10 * 60
    assert ttl_for_intent("chain") == 30 * 60
    assert ttl_for_intent("etf_view") == 60 * 60
    assert ttl_for_intent("nonexistent") == SETTINGS.default_cache_ttl_seconds


def test_expires_at_is_in_the_future():
    now_ms = int(time.time() * 1000)
    e = expires_at_ms("explain_move")
    assert e > now_ms
    assert e <= now_ms + ttl_for_intent("explain_move") * 1000 + 100


@pytest.mark.asyncio
async def test_owner_never_hits_cache(store):
    owner = Caller(kind="owner", ip="1.1.1.1")
    # Pre-seed a "done" session that would otherwise match.
    await store.create_session(
        session_id="seed_abc", text="why nvda", client_kind="guest", ip="9.9.9.9"
    )
    await store.finalize_session(
        session_id="seed_abc",
        intent="explain_move",
        args={"ticker": "NVDA"},
        cache_key=cache_key("explain_move", {"ticker": "NVDA"}),
        status="done",
        reply_text="cached reply",
        total_cost_usd=0.005,
        events=[{"type": "session_start", "ts_ms": 100}],
        expires_at_ms=int(time.time() * 1000) + 10_000_000,
    )

    hit = await try_replay(store, owner, "explain_move", {"ticker": "NVDA"}, "why nvda")
    assert hit is None


@pytest.mark.asyncio
async def test_guest_replay_creates_new_session_pointing_back(store):
    guest = Caller(kind="guest", ip="9.9.9.9")
    await store.create_session(
        session_id="seed_xyz", text="why nvda", client_kind="guest", ip="9.9.9.9"
    )
    await store.finalize_session(
        session_id="seed_xyz",
        intent="explain_move",
        args={"ticker": "NVDA"},
        cache_key=cache_key("explain_move", {"ticker": "NVDA"}),
        status="done",
        reply_text="cached reply",
        total_cost_usd=0.005,
        events=[
            {"type": "session_start", "ts_ms": 100},
            {"type": "llm_call", "ts_ms": 800, "cost_usd": 0.003},
            {"type": "session_end", "ts_ms": 1500},
        ],
        expires_at_ms=int(time.time() * 1000) + 10_000_000,
    )

    hit = await try_replay(
        store, guest, "explain_move", {"ticker": "NVDA"}, "why nvda again"
    )
    assert hit is not None
    assert hit["cached_from"] == "seed_xyz"
    assert hit["reply_text"] == "cached reply"

    # New session row should exist with cached_from pointer.
    replayed = await store.get_session(hit["session_id"])
    assert replayed is not None
    assert replayed["cached_from"] == "seed_xyz"
    # Events are copied over verbatim.
    assert json.loads(replayed["events_json"])[1]["cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_expired_cache_is_a_miss(store):
    guest = Caller(kind="guest", ip="9.9.9.9")
    await store.create_session(
        session_id="seed_old", text="why nvda", client_kind="guest", ip="9.9.9.9"
    )
    await store.finalize_session(
        session_id="seed_old",
        intent="explain_move",
        args={"ticker": "NVDA"},
        cache_key=cache_key("explain_move", {"ticker": "NVDA"}),
        status="done",
        reply_text="cached reply",
        total_cost_usd=0.005,
        events=[],
        expires_at_ms=int(time.time() * 1000) - 1,  # already expired
    )
    hit = await try_replay(store, guest, "explain_move", {"ticker": "NVDA"}, "x")
    assert hit is None
