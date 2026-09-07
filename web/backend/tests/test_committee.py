"""Lab · 投资人委员会 endpoint tests. No network, no key, no LLM."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import committee, workspace
from v2.personas.fixtures import distressed_snapshot, quality_snapshot
from v2.personas.store import PersonaStore


class _FakeClient:
    """Serves the synthetic snapshots through the persona data protocol."""

    def __init__(self):
        self.snaps = {s.ticker: s for s in (quality_snapshot(), distressed_snapshot())}
        self.calls = 0

    def _snap(self, ticker):
        return self.snaps.get(ticker.upper())

    def get_financial_metrics(self, ticker, end_date, *, period="ttm", limit=10):
        self.calls += 1
        s = self._snap(ticker)
        return [r.to_dict() for r in s.metrics(period, limit)] if s else []

    def search_line_items(self, ticker, line_items, end_date, *, period="ttm", limit=10):
        self.calls += 1
        s = self._snap(ticker)
        return [r.to_dict() for r in s.line_items(period, limit)] if s else []

    def get_market_cap(self, ticker, end_date):
        self.calls += 1
        s = self._snap(ticker)
        return s.market_cap if s else None

    def get_insider_trades(self, ticker, end_date, *, start_date=None, limit=1000):
        self.calls += 1
        s = self._snap(ticker)
        return list(s.insider_trades) if s else []

    def get_company_news(self, ticker, end_date, *, start_date=None, limit=100):
        self.calls += 1
        s = self._snap(ticker)
        return list(s.news) if s else []

    def get_prices(self, ticker, start_date, end_date):
        self.calls += 1
        s = self._snap(ticker)
        return list(s.prices) if s else []


@pytest.fixture()
def fake():
    return _FakeClient()


@pytest.fixture()
def client(tmp_path, monkeypatch, fake):

    @contextmanager
    def _fake_data_client():
        yield fake

    monkeypatch.setattr(committee, "_data_client", _fake_data_client)
    return TestClient(app)


def test_committee_on_explicit_tickers_returns_matrix_and_persists(client, fake):
    body = {"source": "tickers", "tickers": ["qlty", "DSTR", "qlty"], "as_of": "2026-06-30", "personas": ["warren_buffett", "ben_graham", "michael_burry"]}
    res = client.post("/api/lab/committee", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["kind"] == "committee" and data["source"] == "tickers"
    assert [v["ticker"] for v in data["verdicts"]] == ["QLTY", "DSTR"]
    assert data["verdicts"][1]["stance"] == "bearish" and data["verdicts"][1]["bearish"] == 3
    assert [m["key"] for m in data["personas_meta"]] == ["warren_buffett", "ben_graham", "michael_burry"]
    assert data["personas_meta"][0]["name_zh"] == "沃伦·巴菲特"
    assert data["top"][0]["ticker"] == "QLTY" and len(data["top"]) == 2
    assert data["cache_hits"] == [] and data["run_id"] and data["data_gaps"] == []
    assert "position" not in data["verdicts"][0]
    first_calls = fake.calls
    assert first_calls > 0

    # persisted: run log + detail + workspace run summary
    runs = client.get("/api/lab/committee/runs").json()["items"]
    assert runs[0]["run_id"] == data["run_id"] and runs[0]["tickers"] == ["QLTY", "DSTR"]
    detail = client.get(f"/api/lab/committee/runs/{data['run_id']}").json()
    assert detail["verdicts"][0]["signals"][0]["persona"] == "warren_buffett"
    lab_runs = client.get("/api/lab/runs").json()["items"]
    assert lab_runs[0]["kind"] == "committee" and lab_runs[0]["top"][0] == "QLTY"
    assert client.get("/api/lab/committee/runs/nope").status_code == 404

    # second run the same day hits the snapshot cache: zero data calls
    again = client.post("/api/lab/committee", json=body).json()
    assert again["cache_hits"] == ["DSTR", "QLTY"] and fake.calls == first_calls
    assert again["verdicts"][0]["consensus"] == data["verdicts"][0]["consensus"]


def test_committee_defaults_to_all_personas_and_rejects_unknown(client):
    res = client.post("/api/lab/committee", json={"tickers": ["QLTY"], "as_of": "2026-06-30"})
    assert res.status_code == 200
    assert len(res.json()["personas_meta"]) == 13 and len(res.json()["verdicts"][0]["signals"]) == 13
    bad = client.post("/api/lab/committee", json={"tickers": ["QLTY"], "personas": ["elon"]})
    assert bad.status_code == 400 and "unknown persona" in bad.json()["detail"]
    assert client.post("/api/lab/committee", json={"tickers": ["bad ticker!"]}).status_code == 400
    assert client.post("/api/lab/committee", json={"tickers": []}).status_code == 400


def test_committee_on_holdings_labels_actions(client, monkeypatch):
    portfolio = {
        "account": {"portfolio_value": 100_000.0},
        "positions": [
            {"symbol": "QLTY", "market_value": 20_000.0, "current_price": 150.0, "side": "PositionSide.LONG", "unrealized_pl_pct": 0.1},
            {"symbol": "DSTR", "market_value": 5_000.0, "current_price": 40.0, "side": "long", "unrealized_pl_pct": -0.2},
            {"symbol": "SHRT", "market_value": 1_000.0, "current_price": 1.0, "side": "PositionSide.SHORT"},
        ],
    }
    import v2.broker.alpaca_client as alpaca

    monkeypatch.setattr(alpaca, "get_portfolio", lambda: portfolio)
    res = client.post("/api/lab/committee", json={"source": "holdings", "as_of": "2026-06-30", "personas": ["warren_buffett", "peter_lynch", "phil_fisher"], "max_weight": 0.15})
    assert res.status_code == 200, res.text
    by = {v["ticker"]: v for v in res.json()["verdicts"]}
    assert set(by) == {"QLTY", "DSTR"}  # shorts are skipped
    assert by["DSTR"]["action"] == "减持候选" and by["DSTR"]["position"]["weight"] == pytest.approx(0.05)
    assert by["QLTY"]["position"]["weight"] == pytest.approx(0.20)
    # QLTY: Lynch + Fisher bullish, Buffett neutral → consensus > .2, agreement 2/3, but weight 20% >= cap 15% → hold
    assert by["QLTY"]["action"] == "持有" and "上限" in by["QLTY"]["action_reason"]
    assert by["QLTY"]["price"] == 150.0

    relaxed = client.post("/api/lab/committee", json={"source": "holdings", "as_of": "2026-06-30", "personas": ["warren_buffett", "peter_lynch", "phil_fisher"], "max_weight": 0.5}).json()
    assert {v["ticker"]: v["action"] for v in relaxed["verdicts"]}["QLTY"] == "增持候选"

    monkeypatch.setattr(alpaca, "get_portfolio", lambda: {"account": {}, "positions": []})
    assert client.post("/api/lab/committee", json={"source": "holdings"}).status_code == 400


def test_committee_on_watchlist_and_screening(client, monkeypatch, tmp_path):
    from v2.bot import state as bot_state

    monkeypatch.setattr(bot_state, "_DB_PATH", tmp_path / "bot_state.db")
    assert client.post("/api/lab/committee", json={"source": "watchlist"}).status_code == 400  # empty
    client.post("/api/watchlist", json={"ticker": "dstr", "note": ""})
    res = client.post("/api/lab/committee", json={"source": "watchlist", "as_of": "2026-06-30", "personas": ["warren_buffett"]})
    assert res.status_code == 200 and [v["ticker"] for v in res.json()["verdicts"]] == ["DSTR"]

    monkeypatch.setattr(
        workspace, "_run_screening",
        lambda body: {"kind": "screening", "date": "2026-06-30", "universe_size": 30, "candidates": [{"ticker": "QLTY", "price": 150.0, "market_cap": 250e9, "revenue_growth": 0.12, "gross_margin": 0.62}]},
    )
    res = client.post("/api/lab/committee", json={"source": "screening", "as_of": "2026-06-30", "personas": ["warren_buffett"], "screening": {"revenue_growth_min": 0.1}})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["screening"]["n_candidates"] == 1 and data["screening"]["universe_size"] == 30
    assert [v["ticker"] for v in data["verdicts"]] == ["QLTY"]


def test_personas_and_scoreboard_endpoints(client):
    items = client.get("/api/lab/committee/personas").json()["items"]
    assert len(items) == 13 and items[0] == {
        "key": "warren_buffett", "name": "Warren Buffett", "name_zh": "沃伦·巴菲特",
        "style": "seeks wonderful companies at a fair price", "period": "ttm", "lookback": 10, "needs": [],
    }
    board = client.get("/api/lab/committee/scoreboard").json()
    assert board["kind"] == "scoreboard" and board["items"] == []
    assert board["counts"] == {"runs": 0, "tickers": 0, "votes": 0, "scored_1m": 0, "scored_3m": 0, "due_1m": 0, "due_3m": 0}
    client.post("/api/lab/committee", json={"tickers": ["QLTY"], "as_of": "2026-06-30", "personas": ["warren_buffett", "ben_graham"]})
    counts = client.get("/api/lab/committee/scoreboard").json()["counts"]
    assert counts["runs"] == 1 and counts["tickers"] == 1 and counts["votes"] == 2 and counts["due_1m"] == 2


def test_store_forward_return_backfill(tmp_path):
    store = PersonaStore(tmp_path / "p.db")
    payload = {
        "as_of": "2026-01-15", "personas": ["warren_buffett"], "elapsed_s": 0.1,
        "verdicts": [{"ticker": "AAA", "price": 100.0, "signals": [
            {"persona": "warren_buffett", "as_of": "2026-01-15", "signal": "bullish", "confidence": 70, "score": 8, "max_score": 10, "abstained": False, "facts": {}},
            {"persona": "ben_graham", "as_of": "2026-01-15", "signal": "neutral", "confidence": 0, "score": 0, "max_score": 0, "abstained": True, "facts": {}},
        ]}],
    }
    run_id = store.save_run(payload, source="tickers")
    assert store.get_run(run_id)["run_id"] == run_id
    pending = store.signals_awaiting_forward_returns(older_than_days=30)
    assert [p["persona"] for p in pending] == ["warren_buffett"]  # abstentions never scored
    store.set_forward_return(pending[0]["id"], column="fwd_1m", value=0.08)
    assert store.signals_awaiting_forward_returns(older_than_days=30) == []
    board = store.persona_scoreboard()
    assert board == [{"persona": "warren_buffett", "n": 1, "hits": 1, "hit_rate": 1.0, "avg_directional_1m": 0.08}]
    latest = store.latest_signals("AAA")
    assert {s["persona"] for s in latest} == {"warren_buffett", "ben_graham"}
    with pytest.raises(ValueError):
        store.set_forward_return(1, column="drop table", value=0)


# ------------------------------------------------------------------ narration

def test_narrate_endpoint_adds_grounded_text_without_changing_the_verdict(client, monkeypatch):
    import json as _json

    from v2.agent.llm import LLMResponse, ScriptedLLM

    run = client.post("/api/lab/committee", json={"tickers": ["QLTY"], "as_of": "2026-06-30", "personas": ["warren_buffett"]}).json()
    sig = run["verdicts"][0]["signals"][0]
    roe_line = sig["parts"][0]["details"].split(";")[0]
    reply = _json.dumps({"signal": sig["signal"], "confidence": sig["confidence"], "reasoning": f"{roe_line}，估值偏贵，先观望。"})
    monkeypatch.setattr(committee, "_narrate_llm", lambda: ScriptedLLM([LLMResponse(text=reply)]))

    res = client.post("/api/lab/committee/narrate", json={"run_id": run["run_id"], "ticker": "qlty", "persona": "warren_buffett"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["narrative"].startswith(roe_line) and data["narrative_grounded"] is True
    assert data["signal"] == sig["signal"] and data["confidence"] == sig["confidence"]
    # persisted into the stored run
    stored = client.get(f"/api/lab/committee/runs/{run['run_id']}").json()
    assert stored["verdicts"][0]["signals"][0]["narrative"] == data["narrative"]

    # a reply that flips the signal is discarded → 503 with the reason
    flipped = _json.dumps({"signal": "bearish" if sig["signal"] != "bearish" else "bullish", "confidence": sig["confidence"], "reasoning": "nope"})
    monkeypatch.setattr(committee, "_narrate_llm", lambda: ScriptedLLM([LLMResponse(text=flipped)]))
    res = client.post("/api/lab/committee/narrate", json={"run_id": run["run_id"], "ticker": "QLTY", "persona": "warren_buffett"})
    assert res.status_code == 503 and "rule-based reasoning stands" in res.json()["detail"]

    assert client.post("/api/lab/committee/narrate", json={"run_id": "nope", "ticker": "QLTY", "persona": "warren_buffett"}).status_code == 404
    assert client.post("/api/lab/committee/narrate", json={"run_id": run["run_id"], "ticker": "QLTY", "persona": "ben_graham"}).status_code == 404


# ------------------------------------------------------------------- backfill

class _Prices:
    """Deterministic price path: 100 on day 0, +0.1 per calendar day."""

    def __init__(self):
        self.calls = 0

    def get_prices(self, ticker, start, end):
        from datetime import date, timedelta

        self.calls += 1
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
        base = date(2026, 1, 1)
        return [{"time": (d0 + timedelta(days=i)).isoformat(), "close": 100 + 0.1 * ((d0 + timedelta(days=i)) - base).days} for i in range((d1 - d0).days + 1) if (d0 + timedelta(days=i)).weekday() < 5]


def test_forward_backfill_fills_due_horizons_and_scores_personas(tmp_path):
    from datetime import date

    from v2.personas.forward import backfill_forward_returns

    store = PersonaStore(tmp_path / "p.db")
    payload = {"as_of": "2026-01-15", "personas": ["a", "b"], "verdicts": [{"ticker": "AAA", "signals": [
        {"persona": "a", "as_of": "2026-01-15", "signal": "bullish", "confidence": 70, "score": 8, "max_score": 10, "abstained": False, "facts": {}},
        {"persona": "b", "as_of": "2026-01-15", "signal": "bearish", "confidence": 60, "score": 2, "max_score": 10, "abstained": False, "facts": {}},
    ]}]}
    store.save_run(payload, source="tickers")
    prices = _Prices()

    # 45 days later: 1m due, 3m not yet
    report = backfill_forward_returns(store, prices, today=date(2026, 3, 1))
    assert report.filled == 2 and report.by_column == {"fwd_1m": 2} and prices.calls == 1
    board = {row["persona"]: row for row in store.persona_scoreboard()}
    assert board["a"]["hits"] == 1 and board["b"]["hits"] == 0  # price rose: bull right, bear wrong
    assert board["a"]["avg_directional_1m"] == pytest.approx(0.0296, abs=0.002)

    # idempotent: nothing left for 1m, 3m fills once due
    again = backfill_forward_returns(store, prices, today=date(2026, 3, 1))
    assert again.filled == 0
    later = backfill_forward_returns(store, prices, today=date(2026, 6, 1))
    assert later.by_column == {"fwd_3m": 2}
    assert store.signals_awaiting_forward_returns(older_than_days=91, column="fwd_3m") == []


def test_backfill_endpoint_reports_and_returns_scoreboard(client, monkeypatch):
    from v2.personas import forward as forward_mod

    monkeypatch.setattr(forward_mod, "_default_price_source", lambda: _Prices())
    res = client.post("/api/lab/committee/backfill", json={"columns": ["fwd_1m"]})
    assert res.status_code == 200, res.text
    assert res.json()["kind"] == "backfill" and res.json()["checked"] == 0 and res.json()["scoreboard"] == []



# ------------------------------------------------------------------- lab store

def test_lab_runs_persist_across_kinds_and_reopen(client, monkeypatch):
    monkeypatch.setattr(workspace, "_run_screening", lambda body: {"kind": "screening", "universe": body.universe, "tickers": ["QLTY"], "universe_size": 1, "candidates": [{"ticker": "QLTY"}]})
    scr = client.post("/api/lab/screening", json={"universe": "custom", "tickers": ["qlty"], "revenue_growth_min": 0.1}).json()
    com = client.post("/api/lab/committee", json={"tickers": ["QLTY"], "as_of": "2026-06-30", "personas": ["warren_buffett"]}).json()
    assert scr["lab_run_id"] and com["lab_run_id"] and scr["lab_run_id"] != com["lab_run_id"]

    runs = client.get("/api/lab/runs").json()
    assert [r["kind"] for r in runs["items"]] == ["committee", "screening"]
    assert runs["counts"] == {"committee": 1, "screening": 1}
    assert runs["items"][1]["n_candidates"] == 1 and runs["items"][1]["candidates"] == ["QLTY"]
    assert runs["items"][0]["stances"]["neutral"] + runs["items"][0]["stances"]["bearish"] + runs["items"][0]["stances"]["bullish"] == 1

    detail = client.get(f"/api/lab/runs/{scr['lab_run_id']}").json()
    assert detail["kind"] == "screening" and detail["params"]["revenue_growth_min"] == 0.1 and detail["result"]["candidates"][0]["ticker"] == "QLTY"
    assert client.get("/api/lab/runs/nope").status_code == 404
    assert [r["kind"] for r in client.get("/api/lab/runs?kind=screening").json()["items"]] == ["screening"]


def test_universe_resolution_for_lab_engines(client, monkeypatch):
    import v2.broker.alpaca_client as alpaca
    from app import sources

    monkeypatch.setattr(alpaca, "get_portfolio", lambda: {"account": {"portfolio_value": 100.0}, "positions": [{"symbol": "AAA", "market_value": 50.0, "side": "long"}, {"symbol": "SHRT", "market_value": 1.0, "side": "short"}]})
    assert sources.resolve_universe("holdings")[0] == ["AAA"]
    assert sources.resolve_universe("tech30")[0][:2] == ["AAPL", "MSFT"]
    assert sources.resolve_universe("custom", ["nvda", "nvda", "amd"])[0] == ["NVDA", "AMD"]
    with pytest.raises(ValueError):
        sources.resolve_universe("custom", [])

    seen = {}
    monkeypatch.setattr(workspace, "_run_backtest", lambda body: seen.setdefault("body", body) and {"kind": "backtest", "strategy": body.strategy, "universe": body.universe, "tickers": ["AAA"], "metrics": {"n_trades": 1}})
    res = client.post("/api/lab/backtest", json={"universe": "holdings", "holding_days": 7})
    assert res.status_code == 200 and seen["body"].universe == "holdings" and seen["body"].holding_days == 7
    assert client.post("/api/lab/backtest", json={"universe": "nowhere"}).status_code == 422
