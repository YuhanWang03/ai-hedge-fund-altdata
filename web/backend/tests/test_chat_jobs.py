from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app import dispatch as dispatch_module
from app.main import app
from app.routers import chat as chat_router


def _poll(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 2
    while time.time() < deadline:
        payload = client.get(f"/api/chat/jobs/{job_id}").json()
        if payload["status"] != "running":
            return payload
        time.sleep(0.02)
    raise AssertionError("chat job did not complete")


def test_chain_runs_as_background_job(monkeypatch):
    chat_router._CHAT_JOBS.clear()
    monkeypatch.setattr(
        dispatch_module,
        "dispatch",
        lambda parsed: {
            "html": f"chain result for {parsed['ticker']}",
            "data": {"seeds": [parsed["ticker"]], "neighbors": []},
        },
    )

    with TestClient(app) as client:
        started = client.post("/api/chat", json={"text": "/chain AAPL"})
        assert started.status_code == 200
        assert started.json()["status"] == "running"

        completed = _poll(client, started.json()["job_id"])
        assert completed["status"] == "completed"
        assert completed["intent"] == "chain"
        assert completed["html"] == "chain result for AAPL"
        assert completed["data"]["seeds"] == ["AAPL"]


def test_duplicate_running_chain_is_deduplicated(monkeypatch):
    chat_router._CHAT_JOBS.clear()
    release = threading.Event()

    def slow_dispatch(parsed):
        release.wait(timeout=1)
        return {"html": f"done {parsed['ticker']}"}

    monkeypatch.setattr(dispatch_module, "dispatch", slow_dispatch)

    with TestClient(app) as client:
        first = client.post("/api/chat", json={"text": "/chain TSLA"}).json()
        second = client.post("/api/chat", json={"text": "/chain TSLA"}).json()
        assert first["job_id"] == second["job_id"]
        assert second["deduplicated"] is True

        release.set()
        assert _poll(client, first["job_id"])["status"] == "completed"
