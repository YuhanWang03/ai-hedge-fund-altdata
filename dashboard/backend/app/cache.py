"""Replay cache for guest queries.

Lookup is keyed by sha256(intent + canonical(args)). Cache hits return a
fresh session_id whose events_json is a copy of the source's events plus
cached_from / cached_at_ms pointers so the SSE handler can do timed replay.

Owner never reads from cache (always fresh execution).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from app.auth import Caller
from app.config import SETTINGS
from app.db.store import Store


def cache_key(intent: str, args: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"intent": intent, "args": args or {}},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ttl_for_intent(intent: str) -> int:
    return SETTINGS.cache_ttl_seconds.get(intent, SETTINGS.default_cache_ttl_seconds)


def expires_at_ms(intent: str) -> int:
    return int(time.time() * 1000) + ttl_for_intent(intent) * 1000


async def try_replay(
    store: Store,
    caller: Caller,
    intent: str,
    args: dict[str, Any] | None,
    text: str,
) -> dict[str, Any] | None:
    """If a non-expired cached run exists, create a replay session and return
    its metadata. Returns None on miss or if caller is owner.
    """
    if caller.is_owner:
        return None
    key = cache_key(intent, args)
    now_ms = int(time.time() * 1000)
    source = await store.lookup_cache(key, now_ms)
    if source is None:
        return None

    replay_session_id = f"sess_{uuid.uuid4().hex[:16]}"
    await store.insert_replay_session(
        session_id=replay_session_id,
        source=source,
        text=text,
        client_kind=caller.kind,
        ip=caller.ip,
    )
    return {
        "session_id": replay_session_id,
        "cached_from": source["session_id"],
        "cached_at_ms": source["created_ms"],
        "reply_text": source["reply_text"],
        "total_cost_usd": source["total_cost_usd"],
    }
