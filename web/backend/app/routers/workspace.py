"""Unified endpoints consumed by the Core / Research / Lab workbench.

The existing dashboard endpoints remain unchanged.  This router exposes the
remaining production state (push feed, watchlist, price alerts) and gives the
already-existing offline research engines a small, validated HTTP surface.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.auth import require_owner
from app.config import SETTINGS
from app.lab_store import LabRunStore
from app.sources import MAX_TICKERS, Universe, normalize_tickers, resolve_universe
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
                        "total_return_pct": m.get("total_return_pct"), "sharpe_ratio": m.get("sharpe_ratio"), "max_drawdown_pct": m.get("max_drawdown_pct")})
    elif kind == "event_study":
        summary.update({"universe": result.get("universe"), "n_events": len(result.get("events") or []), "n_groups": len(result.get("aggregates") or [])})
    elif kind == "screening":
        summary.update({"universe": result.get("universe"), "universe_size": result.get("universe_size"), "n_candidates": len(result.get("candidates") or []),
                        "candidates": [c.get("ticker") for c in (result.get("candidates") or [])][:20]})
    elif kind == "committee":
        verdicts = result.get("verdicts") or []
        summary.update({"run_id": result.get("run_id"), "source": result.get("source"), "n_tickers": len(verdicts),
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
    strategy: Literal["pead"] = "pead"
    holding_days: int = Field(default=5, ge=1, le=60)
    earnings_limit: int = Field(default=8, ge=1, le=20)
    capital: float = Field(default=100_000, gt=0, le=100_000_000)
    per_trade: float = Field(default=10_000, gt=0, le=10_000_000)


def _run_backtest(body: BacktestInput) -> dict:
    from v2.backtesting import BacktestEngine, PEADStrategy
    from v2.data import CachedFDClient

    tickers, meta = resolve_universe(body.universe, body.tickers)
    with CachedFDClient() as client:
        result = BacktestEngine(capital=body.capital, per_trade=body.per_trade).run(
            PEADStrategy(earnings_limit=body.earnings_limit, holding_days=body.holding_days),
            tickers,
            client,
        )
    return {"kind": "backtest", "strategy": body.strategy, "universe": meta["universe"], "tickers": tickers,
            "params": {"holding_days": body.holding_days, "earnings_limit": body.earnings_limit, "capital": body.capital, "per_trade": body.per_trade},
            **result.model_dump()}


class EventStudyInput(BaseModel):
    universe: Universe = "custom"
    tickers: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"], max_length=MAX_TICKERS)
    earnings_limit: int = Field(default=8, ge=1, le=20)
    n_bootstrap: int = Field(default=2000, ge=100, le=10_000)
    require_eps_surprise: bool = True


def _run_event_study(body: EventStudyInput) -> dict:
    from v2.data import CachedFDClient
    from v2.event_study import compute_car

    tickers, meta = resolve_universe(body.universe, body.tickers, limit=20)
    with CachedFDClient() as client:
        result = compute_car(
            tickers,
            client,
            earnings_limit=body.earnings_limit,
            n_bootstrap=body.n_bootstrap,
            require_eps_surprise=body.require_eps_surprise,
        )
    return {"kind": "event_study", "universe": meta["universe"], "tickers": tickers,
            "params": {"earnings_limit": body.earnings_limit, "n_bootstrap": body.n_bootstrap, "require_eps_surprise": body.require_eps_surprise},
            **result.model_dump()}


class ScreeningInput(BaseModel):
    universe: Universe = "tech30"
    tickers: list[str] = Field(default_factory=list, max_length=MAX_TICKERS)
    market_cap_min: float = Field(default=10_000_000_000, ge=0)
    market_cap_max: float = Field(default=5_000_000_000_000, gt=0)
    revenue_growth_min: float = Field(default=0.05, ge=-1, le=10)
    gross_margin_min: float = Field(default=0.50, ge=-1, le=1)
    volatility_max: float = Field(default=0.60, gt=0, le=10)


def _run_screening(body: ScreeningInput) -> dict:
    from v2.data import CachedFDClient
    from v2.screening import FilterConfig, run_screening

    tickers, meta = resolve_universe(body.universe, body.tickers)
    config = FilterConfig(
        market_cap_min=body.market_cap_min,
        market_cap_max=body.market_cap_max,
        revenue_growth_min=body.revenue_growth_min,
        gross_margin_min=body.gross_margin_min,
        volatility_max=body.volatility_max,
    )
    with CachedFDClient() as client:
        result = run_screening(tickers, client, config)
    return {"kind": "screening", "universe": meta["universe"], "tickers": tickers, "thresholds": config.model_dump(), **result.model_dump()}


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
async def run_backtest(body: BacktestInput) -> dict:
    result = await _lab_call(_run_backtest, body)
    _remember_run("backtest", result, body.model_dump())
    return result


@router.post("/lab/event-study")
async def run_event_study(body: EventStudyInput) -> dict:
    result = await _lab_call(_run_event_study, body)
    _remember_run("event_study", result, body.model_dump())
    return result


@router.post("/lab/screening")
async def run_screening(body: ScreeningInput) -> dict:
    result = await _lab_call(_run_screening, body)
    _remember_run("screening", result, body.model_dump())
    return result


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
