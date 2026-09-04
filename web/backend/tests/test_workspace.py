from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import dashboard, workspace
from v2.bot import state as bot_state


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_state, "_DB_PATH", tmp_path / "bot_state.db")
    workspace._LAB_RUNS.clear()
    return TestClient(app)


def test_watchlist_and_price_alert_crud(client: TestClient):
    added = client.post("/api/watchlist", json={"ticker": "nvda", "note": "core"})
    assert added.status_code == 200
    assert added.json()["items"][0]["ticker"] == "NVDA"

    alert = client.post(
        "/api/price-alerts",
        json={"ticker": "nvda", "direction": "above", "target_price": 200},
    )
    assert alert.status_code == 200
    alert_id = alert.json()["id"]
    assert alert.json()["items"][0]["target_price"] == 200

    assert client.delete("/api/watchlist/NVDA").json()["items"] == []
    assert client.delete(f"/api/price-alerts/{alert_id}").json()["items"] == []


def test_activity_reads_archive(client: TestClient, tmp_path, monkeypatch):
    archive_path = tmp_path / "archive.db"
    with sqlite3.connect(archive_path) as conn:
        conn.execute(
            """CREATE TABLE pushes (
                id INTEGER PRIMARY KEY, ts TEXT, agent TEXT, msg_type TEXT,
                tickers TEXT, text_html TEXT, title TEXT,
                priority_tier TEXT, importance_score REAL
            )"""
        )
        conn.execute(
            "INSERT INTO pushes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                datetime.now(timezone.utc).isoformat(),
                "monitor",
                "intraday_anomaly",
                "NVDA",
                "<b>Volume spike</b>",
                "NVDA anomaly",
                "P1",
                82,
            ),
        )
        conn.execute(
            "INSERT INTO pushes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                2,
                datetime.now(timezone.utc).isoformat(),
                "sec",
                "text",
                "AAPL",
                "<b>Form 4 digest</b>",
                "SEC update",
                "P2",
                40,
            ),
        )
    monkeypatch.setattr(
        workspace,
        "SETTINGS",
        SimpleNamespace(archive_db_path=archive_path),
    )

    response = client.get("/api/activity")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2

    realtime = client.get("/api/activity?realtime_only=true")
    assert realtime.status_code == 200
    assert [item["title"] for item in realtime.json()["items"]] == ["NVDA anomaly"]


def test_lab_signals_and_run_log(client: TestClient, monkeypatch):
    signals = client.get("/api/lab/signals")
    assert signals.status_code == 200
    assert signals.json()["monitoring"]["volume_spike_threshold"] == 3.0

    monkeypatch.setattr(
        workspace,
        "_run_backtest",
        lambda body: {
            "kind": "backtest",
            "tickers": body.tickers,
            "metrics": {"n_trades": 3, "sharpe": 1.2},
        },
    )
    result = client.post("/api/lab/backtest", json={"tickers": ["AAPL"]})
    assert result.status_code == 200
    assert result.json()["metrics"]["n_trades"] == 3
    runs = client.get("/api/lab/runs").json()["items"]
    assert runs[0]["kind"] == "backtest"
    assert runs[0]["tickers"] == ["AAPL"]


def test_ticker_validation():
    assert workspace._normalize_tickers([" nvda ", "NVDA", "BRK.B"]) == [
        "NVDA",
        "BRK.B",
    ]
    with pytest.raises(ValueError):
        workspace._normalize_tickers(["123"])


def test_ticker_tape_drops_non_finite_provider_values(monkeypatch):
    from v2.macro import fred_client, market_client

    quotes = {
        "SPY": {"value": float("nan"), "pct_change_1d": 0.01},
        "QQQ": {"value": 500, "pct_change_1d": float("inf")},
    }
    monkeypatch.setattr(market_client, "_safe_quote", lambda symbol: quotes.get(symbol))
    monkeypatch.setattr(fred_client, "get_latest_value", lambda _series: float("nan"))

    result = dashboard._fetch_tape()

    assert result == {
        "items": [{"label": "纳指100", "value": 500.0,
                   "change_pct": None, "unit": ""}],
    }
