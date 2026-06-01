"""Tests for the auto-push (archive.db feed) endpoints.

Builds a synthetic archive.db so we don't need the real one. Verifies:
- /api/recent_pushes lists rows owner-only, 403s guests
- /api/push_trace/{id} returns parsed trace events
- 404 on missing push id
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def synthetic_archive(tmp_path, monkeypatch):
    """Create a fake hedge-fund repo with a populated archive.db at
    data/archive.db and point SETTINGS at it.
    """
    repo = tmp_path / "fake_repo"
    (repo / "data").mkdir(parents=True)
    db_path = repo / "data" / "archive.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE pushes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            agent TEXT NOT NULL,
            msg_type TEXT NOT NULL,
            text_html TEXT,
            image_path TEXT,
            tickers TEXT,
            trace_json TEXT,
            title TEXT,
            expires_at TEXT
        );
        """
    )
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=2)).isoformat()
    trace_events = [
        {"type": "session_start", "seq": 1, "ts_ms": 0, "text": "anomaly_to_telegram"},
        {"type": "api_call", "seq": 2, "ts_ms": 100, "provider": "tavily", "endpoint": "search"},
        {"type": "session_end", "seq": 3, "ts_ms": 5000},
    ]
    conn.executemany(
        """
        INSERT INTO pushes
            (ts, agent, msg_type, text_html, tickers, trace_json, title, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        # Insertion order = id order (AUTOINCREMENT). The endpoint orders
        # by id DESC, so the last-inserted row appears first.
        [
            (
                (now - timedelta(days=5)).isoformat(),   # row id 1 — past 2-day cutoff
                "screen", "text", "<b>old push</b>", "NVDA",
                None, "科技股筛选 · old", expires,
            ),
            (
                (now - timedelta(hours=1)).isoformat(),  # row id 2
                "institutional", "text", "<b>Berkshire 13F</b>", "BRK",
                None, "13F · Berkshire", expires,
            ),
            (
                (now - timedelta(minutes=10)).isoformat(),  # row id 3 — newest
                "anomaly", "photo", "<b>IBM anomaly</b>", "IBM",
                json.dumps(trace_events), "异动 · IBM", expires,
            ),
        ],
    )
    conn.commit()
    conn.close()

    from app.config import SETTINGS
    original = SETTINGS.hedge_fund_repo_path
    object.__setattr__(SETTINGS, "hedge_fund_repo_path", str(repo))
    try:
        yield {"repo": repo, "db": db_path, "now": now}
    finally:
        object.__setattr__(SETTINGS, "hedge_fund_repo_path", original)


@pytest.fixture
async def client(tmp_db, synthetic_archive):
    from app.main import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_recent_pushes_requires_owner(client):
    r = await client.get("/api/recent_pushes")
    assert r.status_code == 403
    assert "owner-only" in r.json()["detail"]


@pytest.mark.asyncio
async def test_recent_pushes_returns_recent_rows(client):
    r = await client.get(
        "/api/recent_pushes",
        headers={"X-Owner-Token": "test-owner-token"},
    )
    assert r.status_code == 200
    body = r.json()
    pushes = body["pushes"]
    # The two recent rows; the 5-day-old row is past the 2-day cutoff.
    assert len(pushes) == 2
    # Newest first (id DESC).
    assert pushes[0]["agent"] == "anomaly"
    assert pushes[0]["title"] == "异动 · IBM"
    assert pushes[0]["has_trace"] == 1
    assert pushes[1]["agent"] == "institutional"
    assert pushes[1]["has_trace"] == 0
    # Preview is truncated.
    assert pushes[0]["preview"].startswith("<b>IBM anomaly</b>")


@pytest.mark.asyncio
async def test_recent_pushes_days_param(client):
    # days=10 brings the old row back.
    r = await client.get(
        "/api/recent_pushes?days=10",
        headers={"X-Owner-Token": "test-owner-token"},
    )
    assert r.status_code == 200
    assert len(r.json()["pushes"]) == 3


@pytest.mark.asyncio
async def test_push_trace_returns_parsed_events(client):
    # Row id=3 — the anomaly push with trace_json populated.
    r = await client.get(
        "/api/push_trace/3",
        headers={"X-Owner-Token": "test-owner-token"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["push"]["agent"] == "anomaly"
    assert body["push"]["text_html"] == "<b>IBM anomaly</b>"
    assert len(body["events"]) == 3
    assert body["events"][0]["type"] == "session_start"
    assert body["events"][1]["provider"] == "tavily"


@pytest.mark.asyncio
async def test_push_trace_no_trace_json_returns_empty_events(client):
    # Row id=2 — institutional push with trace_json IS NULL.
    r = await client.get(
        "/api/push_trace/2",
        headers={"X-Owner-Token": "test-owner-token"},
    )
    assert r.status_code == 200
    assert r.json()["events"] == []


@pytest.mark.asyncio
async def test_push_trace_404(client):
    r = await client.get(
        "/api/push_trace/999",
        headers={"X-Owner-Token": "test-owner-token"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_push_trace_requires_owner(client):
    r = await client.get("/api/push_trace/3")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_push_trace_injects_explanations_for_cron_events(synthetic_archive):
    """Cron scripts emit events directly into trace.events without going
    through the dashboard executor's sink, so the explanation field is
    absent in the saved trace_json. The endpoint must inject it on read.
    """
    # Append a fresh row with 4 ark_csv api_call events that DON'T already
    # carry an explanation field.
    import sqlite3
    cron_events = [
        {"type": "session_start", "session_id": "c1", "seq": 1, "ts_ms": 0,
         "text": "(自动推送) ARKK"},
        {"type": "api_call", "session_id": "c1", "seq": 2, "ts_ms": 100,
         "provider": "ark_csv", "endpoint": "fetch_holdings", "ticker": "ARKK"},
        {"type": "api_call", "session_id": "c1", "seq": 3, "ts_ms": 200,
         "provider": "ark_csv", "endpoint": "fetch_holdings", "ticker": "ARKW"},
        {"type": "transform", "session_id": "c1", "seq": 4, "ts_ms": 300,
         "op": "etf_diff", "etf": "ARKK"},
        {"type": "db_write", "session_id": "c1", "seq": 5, "ts_ms": 400,
         "db": "etf.db", "fn": "save_snapshot"},
        {"type": "render", "session_id": "c1", "seq": 6, "ts_ms": 500,
         "card": "etf_snapshot"},
        {"type": "session_end", "session_id": "c1", "seq": 7, "ts_ms": 600},
    ]
    import json as _json
    conn = sqlite3.connect(str(synthetic_archive["db"]))
    conn.execute(
        "INSERT INTO pushes (ts, agent, msg_type, text_html, tickers, "
        "trace_json, title, expires_at) VALUES (?, ?, 'text', ?, ?, ?, ?, ?)",
        (
            synthetic_archive["now"].isoformat(),
            "etf",
            "<b>ARK 每日持仓</b>",
            "ARKK,ARKW",
            _json.dumps(cron_events),
            "ARK 每日持仓",
            (synthetic_archive["now"] + __import__("datetime").timedelta(days=2)).isoformat(),
        ),
    )
    conn.commit()
    new_id = conn.execute("SELECT MAX(id) FROM pushes").fetchone()[0]
    conn.close()

    from app.main import app
    from httpx import ASGITransport, AsyncClient
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.get(
                f"/api/push_trace/{new_id}",
                headers={"X-Owner-Token": "test-owner-token"},
            )
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event that has a catalogue entry must now carry .explanation.
    api_evs = [e for e in events if e["type"] == "api_call"]
    assert all("explanation" in e for e in api_evs), \
        f"api_call events missing explanation: {api_evs}"
    # The transform/etf_diff has an entry.
    diff = next(e for e in events if e["type"] == "transform" and e["op"] == "etf_diff")
    assert "explanation" in diff
    assert "ARK" in diff["explanation"]["source"] or "持仓" in diff["explanation"]["source"]
    # And the render card.
    render = next(e for e in events if e["type"] == "render" and e["card"] == "etf_snapshot")
    assert "explanation" in render
