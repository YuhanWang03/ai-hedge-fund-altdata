"""Unified endpoints consumed by the Core / Research / Lab workbench.

The existing dashboard endpoints remain unchanged.  This router exposes the
remaining production state (push feed, watchlist, price alerts) and gives the
already-existing offline research engines a small, validated HTTP surface.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.auth import require_owner
from app.config import SETTINGS
from app.fd_pricing import cost as fd_cost
from app.screening import CRITERIA, DEFAULT_RULES, Rule, screen as lab_screen
from app.lab_store import LabRunStore
from app.sources import BIG_LIMIT, INDEX_UNIVERSES, MAX_TICKERS, Universe, normalize_tickers, resolve_universe
from v2.archive.store import recent_trading_day_cutoff_iso

router = APIRouter(prefix="/api", tags=["workspace"], dependencies=[Depends(require_owner)])

_LAB_TIMEOUT_SECONDS = 240
_LAB_STORE: LabRunStore | None = None


def _lab_store() -> LabRunStore:
    global _LAB_STORE
    if _LAB_STORE is None:
        _LAB_STORE = LabRunStore()
    return _LAB_STORE


def _summarize(kind: str, result: dict) -> dict:
    """Compact, list-friendly view of one run; the full result is stored alongside."""
    summary: dict = {"tickers": result.get("tickers", [])}
    if kind == "backtest":
        m = result.get("metrics") or {}
        summary.update({"strategy": result.get("strategy"), "universe": result.get("universe"), "n_trades": m.get("n_trades", 0),
                        "total_return_pct": m.get("total_return_pct"), "sharpe_ratio": m.get("sharpe_ratio"), "max_drawdown_pct": m.get("max_drawdown_pct"),
                        "data_source": result.get("data_source"), "fd_cost_usd": result.get("fd_cost_usd"), "excess_return_pct": result.get("excess_return_pct")})
    elif kind == "event_study":
        summary.update({"universe": result.get("universe"), "n_events": len(result.get("events") or []), "n_groups": len(result.get("aggregates") or []),
                        "data_source": result.get("data_source"), "fd_cost_usd": result.get("fd_cost_usd")})
    elif kind == "screening":
        summary.update({"universe": result.get("universe"), "universe_size": result.get("universe_size"), "n_candidates": len(result.get("candidates") or []),
                        "candidates": [c.get("ticker") for c in (result.get("candidates") or [])][:20],
                        "data_source": result.get("data_source"), "fd_cost_usd": result.get("fd_cost_usd")})
    elif kind == "committee":
        verdicts = result.get("verdicts") or []
        summary.update({"run_id": result.get("run_id"), "source": result.get("source"), "n_tickers": len(verdicts), "fd_cost_usd": result.get("fd_cost_usd"),
                        "tickers": [v.get("ticker") for v in verdicts], "top": [t.get("ticker") for t in (result.get("top") or [])[:5]],
                        "stances": {k: sum(1 for v in verdicts if v.get("stance") == k) for k in ("bullish", "bearish", "neutral", "abstain")}})
    elif kind == "backfill":
        summary.update({"checked": result.get("checked"), "filled": result.get("filled")})
    return summary


def _remember_run(kind: str, result: dict, params: dict | None = None) -> str:
    """Persist a run and stamp its id onto the result."""
    run_id = _lab_store().save(kind, params=params, summary=_summarize(kind, result), result=result)
    result["lab_run_id"] = run_id
    return run_id


def _normalize_tickers(values: list[str], *, limit: int = MAX_TICKERS) -> list[str]:
    return normalize_tickers(values, limit=limit)


@router.get("/activity")
async def activity(
    days: int = Query(2, ge=1, le=30),
    limit: int = Query(100, ge=1, le=200),
    realtime_only: bool = Query(False),
) -> dict:
    """Recent archived pushes, used as the real Core alert feed."""
    db_path = SETTINGS.archive_db_path
    if not db_path.exists():
        return {"items": [], "warning": "archive.db not found"}

    def _fetch() -> list[dict]:
        cutoff = (
            recent_trading_day_cutoff_iso(2)
            if realtime_only
            else (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        )
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pushes)")}
            if not columns:
                return []
            optional = {
                "title": "title" if "title" in columns else "NULL AS title",
                "priority_tier": "priority_tier" if "priority_tier" in columns else "NULL AS priority_tier",
                "importance_score": "importance_score" if "importance_score" in columns else "NULL AS importance_score",
            }
            realtime_clause = """
                AND (agent IN ('intraday_anomaly', 'alert', 'anomaly')
                     OR msg_type = 'intraday_anomaly')
            """ if realtime_only else ""
            sql = f"""
                SELECT id, ts, agent, msg_type, tickers,
                       substr(COALESCE(text_html, ''), 1, 1000) AS preview,
                       {optional['title']}, {optional['priority_tier']},
                       {optional['importance_score']}
                FROM pushes
                WHERE ts >= ?
                {realtime_clause}
                ORDER BY ts DESC
                LIMIT ?
            """
            rows = conn.execute(sql, (cutoff, limit)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    try:
        return {"items": await run_in_threadpool(_fetch)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/monitoring/universe")
async def monitoring_universe() -> dict:
    """The production ticker pool scanned by the minute-level streamer."""
    from v2.screening.universe import TECH_30

    return {
        "intraday": list(TECH_30),
        "source": "TECH_30",
        "scan_interval_seconds": 60,
        "price_pct_threshold": 0.03,
        "volume_pace_threshold": 2.5,
    }


class WatchlistInput(BaseModel):
    ticker: str
    note: str = Field(default="", max_length=200)


@router.get("/watchlist")
async def watchlist() -> dict:
    from v2.bot.state import watchlist_list

    return {"items": await run_in_threadpool(watchlist_list)}


@router.post("/watchlist")
async def add_watchlist(body: WatchlistInput) -> dict:
    from v2.bot.state import watchlist_add, watchlist_list

    try:
        added = await run_in_threadpool(watchlist_add, body.ticker, body.note)
        items = await run_in_threadpool(watchlist_list)
        return {"added": added, "items": items}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/watchlist/{ticker}")
async def remove_watchlist(ticker: str) -> dict:
    from v2.bot.state import watchlist_list, watchlist_remove

    removed = await run_in_threadpool(watchlist_remove, ticker)
    return {"removed": removed, "items": await run_in_threadpool(watchlist_list)}


class PriceAlertInput(BaseModel):
    ticker: str
    direction: Literal["above", "below"]
    target_price: float = Field(gt=0)


@router.get("/price-alerts")
async def price_alerts(include_fired: bool = False) -> dict:
    from v2.bot.state import alert_list

    return {"items": await run_in_threadpool(alert_list, include_fired)}


@router.post("/price-alerts")
async def add_price_alert(body: PriceAlertInput) -> dict:
    from v2.bot.state import alert_add, alert_list

    try:
        alert_id = await run_in_threadpool(alert_add, body.ticker, body.direction, body.target_price)
        return {"id": alert_id, "items": await run_in_threadpool(alert_list, False)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/price-alerts/{alert_id}")
async def remove_price_alert(alert_id: int) -> dict:
    from v2.bot.state import alert_list, alert_remove

    removed = await run_in_threadpool(alert_remove, alert_id)
    return {"removed": removed, "items": await run_in_threadpool(alert_list, False)}


class BacktestInput(BaseModel):
    universe: Universe = "custom"
    tickers: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"], max_length=MAX_TICKERS)
    strategy: Literal["pead", "momentum", "insider", "committee"] = "pead"
    #: where daily prices come from; events / fundamentals are always Financial Datasets
    data_source: Literal["yfinance", "fd"] = "yfinance"
    holding_days: int = Field(default=5, ge=1, le=252)
    capital: float = Field(default=100_000, gt=0, le=100_000_000)
    per_trade: float = Field(default=10_000, gt=0, le=10_000_000)
    # pead
    earnings_limit: int = Field(default=8, ge=1, le=20)
    # momentum / insider / committee: how far back signals are generated
    history_days: int = Field(default=730, ge=60, le=3650)
    top_n: int = Field(default=5, ge=1, le=60)
    # momentum
    lookback_days: int = Field(default=252, ge=20, le=504)
    skip_days: int = Field(default=21, ge=0, le=120)
    near_high_pct: float | None = Field(default=None, ge=0, le=1)
    # insider cluster
    window_days: int = Field(default=30, ge=1, le=180)
    min_insiders: int = Field(default=2, ge=1, le=20)
    min_value_usd: float = Field(default=100_000, ge=0)
    # committee
    min_consensus: float = Field(default=0.2, ge=-1, le=1)
    min_agreement: float = Field(default=0.5, ge=0, le=1)
    personas: list[str] | None = Field(default=None, max_length=20)
    lean: bool = True
    filing_lag_days: int = Field(default=45, ge=0, le=120)

    def params(self) -> dict:
        common = {"holding_days": self.holding_days, "capital": self.capital, "per_trade": self.per_trade, "data_source": self.data_source}
        extra = {
            "pead": {"earnings_limit": self.earnings_limit},
            "momentum": {"history_days": self.history_days, "top_n": self.top_n, "lookback_days": self.lookback_days, "skip_days": self.skip_days, "near_high_pct": self.near_high_pct},
            "insider": {"history_days": self.history_days, "window_days": self.window_days, "min_insiders": self.min_insiders, "min_value_usd": self.min_value_usd},
            "committee": {"history_days": self.history_days, "top_n": self.top_n, "min_consensus": self.min_consensus, "min_agreement": self.min_agreement,
                          "personas": self.personas, "lean": self.lean, "filing_lag_days": self.filing_lag_days},
        }[self.strategy]
        return {**common, **extra}


#: FD's /prices endpoint returns at most ~100 bars per request
FD_PRICE_CHUNK_DAYS = 90


@contextmanager
def _data_bundle(data_source: str, *, needs_fd: bool, persona_client: bool = False):
    """Price feed per ``data_source``; the FD client only when the run needs it.

    Shared by the backtest and the event study: prices come from yfinance (free)
    or FD (paid, chunked), events / fundamentals always from FD via ``raw``.
    """
    from v2.backtesting.strategies import BacktestData, PriceCache

    raw = None
    if needs_fd or data_source == "fd":
        from v2.data import CachedFDClient

        raw = CachedFDClient()
        raw.__enter__()
    try:
        if data_source == "fd":
            from v2.data.price_source import FDPriceSource

            prices = PriceCache(FDPriceSource(raw), chunk_days=FD_PRICE_CHUNK_DAYS)
        else:
            from v2.data.price_source import YFinancePriceSource

            prices = PriceCache(YFinancePriceSource())
        fd = None
        if raw is not None and persona_client:
            from v2.personas.data import adapt_client

            fd = adapt_client(raw)
        yield BacktestData(prices=prices, fd=fd, raw=raw)
    finally:
        if raw is not None:
            raw.__exit__(None, None, None)


def _backtest_data(body: BacktestInput):
    return _data_bundle(body.data_source, needs_fd=body.strategy != "momentum", persona_client=body.strategy in ("insider", "committee"))


def _fd_bill(data, data_source: str) -> dict[str, int]:
    """Paid requests by endpoint, including FD price chunks when FD served prices."""
    counts = dict(data.fd_requests)
    if data_source == "fd" and data.prices.requests:
        counts["prices"] = counts.get("prices", 0) + data.prices.requests
    return counts


def _build_strategy(body: BacktestInput, on_tick=None):
    from v2.backtesting import CommitteeStrategy, InsiderClusterStrategy, MomentumStrategy, PEADStrategy

    if body.strategy == "pead":
        return PEADStrategy(earnings_limit=body.earnings_limit, holding_days=body.holding_days)
    if body.strategy == "momentum":
        return MomentumStrategy(lookback_days=body.lookback_days, skip_days=body.skip_days, holding_days=body.holding_days, top_n=body.top_n,
                                history_days=body.history_days, near_high_pct=body.near_high_pct, progress=on_tick)
    if body.strategy == "insider":
        return InsiderClusterStrategy(window_days=body.window_days, min_insiders=body.min_insiders, min_value_usd=body.min_value_usd,
                                      holding_days=body.holding_days, history_days=body.history_days, progress=on_tick)
    from app.routers.committee import _store  # lazy: committee imports this module

    return CommitteeStrategy(holding_days=body.holding_days, history_days=body.history_days, top_n=body.top_n, min_consensus=body.min_consensus,
                             min_agreement=body.min_agreement, personas=body.personas, lean=body.lean, filing_lag_days=body.filing_lag_days,
                             store=_store(), progress=on_tick)


def _backtest_limit(body: BacktestInput) -> int:
    """Price-only momentum is free, so it may run over a whole index; paid strategies keep the small cap."""
    return BIG_LIMIT if body.strategy == "momentum" else MAX_TICKERS


def _benchmark(data, trades, ticker: str = "SPY") -> dict | None:
    """Buy-and-hold return of ``ticker`` from the first entry to the last exit, for comparison."""
    if not trades:
        return None
    start = min(t.entry_date for t in trades)
    end = max(t.exit_date for t in trades)
    rows = data.get_prices(ticker, start, end) or []
    closes = []
    for r in rows:
        close = r.get("close") if isinstance(r, dict) else getattr(r, "close", None)
        try:
            close = float(close)
        except (TypeError, ValueError):
            continue
        if close > 0:
            closes.append(close)
    if len(closes) < 2:
        return None
    total = closes[-1] / closes[0] - 1
    days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
    years = days / 365.25
    annualized = (1 + total) ** (1 / years) - 1 if years >= 0.1 else None
    return {"ticker": ticker, "start": start, "end": end, "total_return_pct": round(total, 6),
            "annualized_return_pct": round(annualized, 6) if annualized is not None else None}


def _backtest_total(body: BacktestInput, n_tickers: int) -> int:
    """Progress units: tickers, or tickers × rebalance dates for the committee."""
    if body.strategy != "committee":
        return n_tickers
    from v2.backtesting.strategies import rebalance_dates

    return n_tickers * max(1, len(rebalance_dates(today=datetime.now(timezone.utc).date(), history_days=body.history_days, step_trading_days=body.holding_days)))


def _run_backtest(body: BacktestInput, on_tick=None) -> dict:
    from v2.backtesting import BacktestEngine

    tickers, meta = resolve_universe(body.universe, body.tickers, limit=_backtest_limit(body))
    with _backtest_data(body) as data:
        data.progress = on_tick
        strategy = _build_strategy(body, on_tick)
        result = BacktestEngine(capital=body.capital, per_trade=body.per_trade).run(strategy, tickers, data)
        benchmark = _benchmark(data, result.trades)
        fd_requests = _fd_bill(data, body.data_source)
        notes = {"price_failures": dict(data.prices.failed), "errors": dict(getattr(strategy, "errors", {}) or {}),
                 "rebalance_dates": list(getattr(strategy, "dates", []) or []), "periods": list(getattr(strategy, "periods", []) or []),
                 "aborted": getattr(strategy, "aborted", None)}
    excess = None
    if benchmark and result.metrics:
        excess = round(result.metrics.total_return_pct - benchmark["total_return_pct"], 6)
    return {"kind": "backtest", "strategy": body.strategy, "data_source": body.data_source, "universe": meta["universe"], "universe_as_of": meta.get("as_of"),
            "tickers": tickers, "params": body.params(), "fd_requests": fd_requests, "fd_cost_usd": fd_cost(fd_requests), "notes": notes,
            "benchmark": benchmark, "excess_return_pct": excess, **result.model_dump()}


class EventStudyInput(BaseModel):
    universe: Universe = "custom"
    tickers: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"], max_length=MAX_TICKERS)
    #: where daily prices (stock and SPY) come from; earnings history is always Financial Datasets
    data_source: Literal["yfinance", "fd"] = "yfinance"
    earnings_limit: int = Field(default=8, ge=1, le=20)
    n_bootstrap: int = Field(default=2000, ge=100, le=10_000)
    require_eps_surprise: bool = True
    #: one event per (ticker, report period): the 8-K and the later 10-Q/10-K are the same announcement
    dedupe: bool = True
    #: "surprise" → ALL / BEAT / MISS / MEET; "reaction" → terciles of the 2-day reaction; "source" → by filing type
    group_by: Literal["surprise", "reaction", "source"] = "surprise"


def _run_event_study(body: EventStudyInput) -> dict:
    from v2.event_study import compute_car

    tickers, meta = resolve_universe(body.universe, body.tickers, limit=20)
    # The bundle answers get_prices (chosen feed) and get_earnings_history (FD, counted), which is all compute_car reads.
    with _data_bundle(body.data_source, needs_fd=True) as data:
        result = compute_car(
            tickers,
            data,
            earnings_limit=body.earnings_limit,
            n_bootstrap=body.n_bootstrap,
            require_eps_surprise=body.require_eps_surprise,
            dedupe=body.dedupe,
            group_by=body.group_by,
        )
        fd_requests = _fd_bill(data, body.data_source)
        price_failures = dict(data.prices.failed)
    return {"kind": "event_study", "universe": meta["universe"], "tickers": tickers, "data_source": body.data_source,
            "params": {"earnings_limit": body.earnings_limit, "n_bootstrap": body.n_bootstrap, "require_eps_surprise": body.require_eps_surprise,
                       "data_source": body.data_source, "dedupe": body.dedupe, "group_by": body.group_by},
            "fd_requests": fd_requests, "fd_cost_usd": fd_cost(fd_requests), "price_failures": price_failures, **result.model_dump()}


class ScreeningInput(BaseModel):
    universe: Universe = "tech30"
    tickers: list[str] = Field(default_factory=list, max_length=MAX_TICKERS)
    #: where the four screening inputs come from. yfinance is free and covers
    #: market cap / revenue growth / gross margin; FD bills per request.
    data_source: Literal["yfinance", "fd"] = "yfinance"
    #: enrich candidates with Wall-Street earnings estimates (one FD request per candidate)
    with_earnings: bool = False
    #: pick-and-mix criteria; omitted → DEFAULT_RULES (the old five-threshold screen minus the cap ceiling)
    rules: list[Rule] | None = Field(default=None, max_length=24)
    # legacy thresholds, still accepted; folded into rules when `rules` is omitted
    market_cap_min: float | None = Field(default=None, ge=0)
    market_cap_max: float | None = Field(default=None, gt=0)
    revenue_growth_min: float | None = Field(default=None, ge=-1, le=10)
    gross_margin_min: float | None = Field(default=None, ge=-1, le=1)
    volatility_max: float | None = Field(default=None, gt=0, le=10)

    def effective_rules(self) -> list[Rule]:
        if self.rules is not None:
            unknown = [r.field for r in self.rules if r.field not in CRITERIA]
            if unknown:
                raise ValueError(f"unknown screening field: {', '.join(unknown)}")
            return list(self.rules)
        legacy = [("market_cap", "gte", self.market_cap_min), ("market_cap", "lte", self.market_cap_max), ("revenue_growth", "gte", self.revenue_growth_min),
                  ("gross_margin", "gte", self.gross_margin_min), ("volatility", "lte", self.volatility_max)]
        picked = [Rule(field=f, op=o, value=v) for f, o, v in legacy if v is not None]
        return picked or list(DEFAULT_RULES)


class _ScreenData:
    """Metrics from one client, earnings (optional) from another, both tolerant.

    Wraps the screener's data dependency so that a ticker the provider does not
    cover is skipped instead of aborting the run, and so that FD requests can be
    counted for the cost line. ``metrics_is_fd`` says whether metrics calls bill.
    """

    def __init__(self, metrics_client, *, metrics_is_fd: bool, earnings_client=None):
        self._metrics = metrics_client
        self._earnings = earnings_client
        self._metrics_is_fd = metrics_is_fd
        self.skipped: dict[str, str] = {}
        self.fd_requests: dict[str, int] = {}

    def _count(self, endpoint: str) -> None:
        self.fd_requests[endpoint] = self.fd_requests.get(endpoint, 0) + 1

    def get_financial_metrics(self, ticker, end_date, limit=1, **kwargs):
        if self._metrics_is_fd:
            self._count("financial_metrics")
        try:
            return self._metrics.get_financial_metrics(ticker, end_date, limit=limit, **kwargs)
        except Exception as exc:  # noqa: BLE001 — provider miss for one ticker
            self.skipped.setdefault(str(ticker), f"metrics: {type(exc).__name__}: {str(exc)[:120]}")
            return []

    def get_earnings(self, ticker):
        if self._earnings is None:
            return None
        self._count("earnings")
        try:
            return self._earnings.get_earnings(ticker)
        except Exception as exc:  # noqa: BLE001
            self.skipped.setdefault(str(ticker), f"earnings: {type(exc).__name__}: {str(exc)[:120]}")
            return None

    def __getattr__(self, name):  # anything else (misses, stats, ...) comes from the metrics client
        return getattr(self._metrics, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for client in (self._metrics, self._earnings):
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        return False


def _screen_clients(body: "ScreeningInput") -> _ScreenData:
    """yfinance (free) or FD for metrics; FD for earnings only when asked."""
    from v2.data import CachedFDClient

    fd = CachedFDClient() if (body.data_source == "fd" or body.with_earnings) else None
    if body.data_source == "yfinance":
        try:
            from v2.data.yfinance_client import YFinanceClient
        except Exception as exc:  # noqa: BLE001 — fall back to FD rather than fail the screen
            raise RuntimeError(f"yfinance client unavailable ({type(exc).__name__}); choose data_source=fd") from exc
        return _ScreenData(YFinanceClient(), metrics_is_fd=False, earnings_client=fd if body.with_earnings else None)
    return _ScreenData(fd, metrics_is_fd=True, earnings_client=fd if body.with_earnings else None)


def _run_screening(body: ScreeningInput, on_tick=None) -> dict:
    from v2.data.price_source import default_price_source

    tickers, meta = resolve_universe(body.universe, body.tickers, limit=BIG_LIMIT)
    rules = body.effective_rules()
    with _screen_clients(body) as client:
        result = lab_screen(tickers, client, default_price_source(), rules, on_tick=on_tick, with_earnings=body.with_earnings)
        skipped, fd_requests = dict(client.skipped), dict(client.fd_requests)
    return {"kind": "screening", "universe": meta["universe"], "universe_as_of": meta.get("as_of"), "tickers": tickers,
            "data_source": body.data_source, "with_earnings": body.with_earnings,
            "fd_requests": fd_requests, "fd_cost_usd": fd_cost(fd_requests), "skipped": skipped, **result}


#: screens bigger than this run as a background job (nginx cuts requests at 90 s)
SCREEN_SYNC_MAX = 40
#: backtests with more progress units than this run as a background job
BACKTEST_SYNC_MAX = 30
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _run_job(job_id: str, kind: str, body, fn) -> None:
    def tick(i: int) -> None:
        with _JOBS_LOCK:
            _JOBS[job_id]["done"] = i

    try:
        result = fn(body, on_tick=tick)
        _remember_run(kind, result, body.model_dump())
        with _JOBS_LOCK:
            _JOBS[job_id].update({"status": "completed", "done": _JOBS[job_id]["total"], "result": result})
    except Exception as exc:  # noqa: BLE001
        with _JOBS_LOCK:
            _JOBS[job_id].update({"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})


def _start_job(kind: str, body, total: int, fn) -> dict:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        if len(_JOBS) > 50:  # keep the in-memory table small
            for old in sorted(_JOBS, key=lambda k: _JOBS[k]["started_at"])[:25]:
                _JOBS.pop(old, None)
        _JOBS[job_id] = {"job_id": job_id, "kind": f"{kind}_job", "status": "running", "done": 0, "total": total,
                         "universe": body.universe, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    threading.Thread(target=_run_job, args=(job_id, kind, body, fn), name=f"{kind}-{job_id}", daemon=True).start()
    return _job_view(job_id)


def _job_view(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else {}


async def _lab_call(fn, body) -> dict:
    try:
        return await asyncio.wait_for(run_in_threadpool(fn, body), timeout=_LAB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="lab run timed out") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/lab/backtest")
async def run_backtest(body: BacktestInput, background: bool | None = None) -> dict:
    """Quick runs answer inline; the committee strategy (or ?background=true) returns a job to poll."""
    try:
        tickers, _ = await run_in_threadpool(resolve_universe, body.universe, body.tickers, limit=_backtest_limit(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    total = _backtest_total(body, len(tickers))
    if background or (background is None and (body.strategy == "committee" or total > BACKTEST_SYNC_MAX)):
        return _start_job("backtest", body, total, _run_backtest)
    result = await _lab_call(_run_backtest, body)
    _remember_run("backtest", result, body.model_dump())
    return result


@router.get("/lab/backtest/jobs/{job_id}")
async def backtest_job(job_id: str) -> dict:
    job = _job_view(job_id)
    if not job or job.get("kind") != "backtest_job":
        raise HTTPException(status_code=404, detail="backtest job not found")
    return job


@router.post("/lab/event-study")
async def run_event_study(body: EventStudyInput) -> dict:
    result = await _lab_call(_run_event_study, body)
    _remember_run("event_study", result, body.model_dump())
    return result


@router.post("/lab/screening")
async def run_screening(body: ScreeningInput, background: bool | None = None) -> dict:
    """Small universes answer inline; large ones (or ?background=true) return a job to poll."""
    try:
        tickers, _ = await run_in_threadpool(resolve_universe, body.universe, body.tickers, limit=BIG_LIMIT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if background or (background is None and len(tickers) > SCREEN_SYNC_MAX):
        return _start_job("screening", body, len(tickers), _run_screening)
    result = await _lab_call(_run_screening, body)
    _remember_run("screening", result, body.model_dump())
    return result


@router.get("/lab/screening/jobs/{job_id}")
async def screening_job(job_id: str) -> dict:
    job = _job_view(job_id)
    if not job or job.get("kind") != "screening_job":
        raise HTTPException(status_code=404, detail="screening job not found")
    return job


@router.get("/lab/screening/criteria")
async def screening_criteria() -> dict:
    """Fields a screening rule may use, with labels / units, and the default rule set."""
    return {"kind": "criteria", "items": CRITERIA, "defaults": [r.model_dump() for r in DEFAULT_RULES]}


@router.get("/lab/universes")
async def universes() -> dict:
    """Named universes with sizes and snapshot dates (index lists refreshable on the VPS)."""
    from v2.screening.universe import TECH_30
    from v2.screening.universes import universe_status

    items = {"tech30": {"size": len(TECH_30), "as_of": None, "label": "TECH_30 监控池"}}
    items.update(universe_status())
    return {"kind": "universes", "items": items}


@router.get("/lab/signals")
async def signal_candidates() -> dict:
    """Current deterministic thresholds behind production anomaly signals."""
    from v2.monitoring.models import MonitorConfig

    monitor = MonitorConfig()
    return {
        "kind": "signals",
        "monitoring": monitor.model_dump(),
        "intraday": {
            "price_pct_threshold": 0.03,
            "volume_pace_threshold": 2.5,
            "sector_gap_pp": 0.015,
            "cooldown_minutes": 30,
        },
        "note": "read-only snapshot; changing Lab UI does not mutate production thresholds",
    }


@router.get("/lab/runs")
async def lab_runs(limit: int = Query(50, ge=1, le=200), kind: str | None = None) -> dict:
    return {"kind": "runs", "items": _lab_store().list(limit=limit, kind=kind), "counts": _lab_store().counts()}


@router.get("/lab/runs/{run_id}")
async def lab_run_detail(run_id: str) -> dict:
    row = _lab_store().get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="lab run not found")
    return row
