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
from v2.archive.store import recent_trading_day_cutoff_iso

router = APIRouter(prefix="/api", tags=["workspace"], dependencies=[Depends(require_owner)])

_LAB_TIMEOUT_SECONDS = 240
_LAB_RUNS: list[dict] = []


def _remember_run(kind: str, result: dict) -> None:
    summary: dict = {"kind": kind, "ran_at": datetime.now(timezone.utc).isoformat()}
    if kind == "backtest":
        summary.update({"n_trades": (result.get("metrics") or {}).get("n_trades", 0), "tickers": result.get("tickers", [])})
    elif kind == "event_study":
        summary.update({"n_events": len(result.get("events") or []), "tickers": result.get("tickers", [])})
    elif kind == "screening":
        summary.update({"n_candidates": len(result.get("candidates") or []), "tickers": result.get("tickers", [])})
    _LAB_RUNS.insert(0, summary)
    del _LAB_RUNS[50:]


def _normalize_tickers(values: list[str], *, limit: int = 30) -> list[str]:
    out: list[str] = []
    for raw in values:
        ticker = raw.strip().upper()
        if not ticker or len(ticker) > 8 or not all(ch.isalpha() or ch in ".-" for ch in ticker):
            raise ValueError(f"invalid ticker: {raw!r}")
        if ticker not in out:
            out.append(ticker)
    if not out:
        raise ValueError("at least one ticker is required")
    return out[:limit]


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
    tickers: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"], max_length=30)
    holding_days: int = Field(default=5, ge=1, le=60)
    earnings_limit: int = Field(default=8, ge=1, le=20)
    capital: float = Field(default=100_000, gt=0, le=100_000_000)
    per_trade: float = Field(default=10_000, gt=0, le=10_000_000)


def _run_backtest(body: BacktestInput) -> dict:
    from v2.backtesting import BacktestEngine, PEADStrategy
    from v2.data import CachedFDClient

    tickers = _normalize_tickers(body.tickers)
    with CachedFDClient() as client:
        result = BacktestEngine(capital=body.capital, per_trade=body.per_trade).run(
            PEADStrategy(earnings_limit=body.earnings_limit, holding_days=body.holding_days),
            tickers,
            client,
        )
    return {"kind": "backtest", "strategy": "pead", "tickers": tickers, **result.model_dump()}


class EventStudyInput(BaseModel):
    tickers: list[str] = Field(default_factory=lambda: ["AAPL", "MSFT", "NVDA"], max_length=20)
    earnings_limit: int = Field(default=8, ge=1, le=20)
    n_bootstrap: int = Field(default=2000, ge=100, le=10_000)
    require_eps_surprise: bool = True


def _run_event_study(body: EventStudyInput) -> dict:
    from v2.data import CachedFDClient
    from v2.event_study import compute_car

    tickers = _normalize_tickers(body.tickers, limit=20)
    with CachedFDClient() as client:
        result = compute_car(
            tickers,
            client,
            earnings_limit=body.earnings_limit,
            n_bootstrap=body.n_bootstrap,
            require_eps_surprise=body.require_eps_surprise,
        )
    return {"kind": "event_study", "tickers": tickers, **result.model_dump()}


class ScreeningInput(BaseModel):
    tickers: list[str] = Field(default_factory=list, max_length=30)
    market_cap_min: float = Field(default=10_000_000_000, ge=0)
    market_cap_max: float = Field(default=5_000_000_000_000, gt=0)
    revenue_growth_min: float = Field(default=0.05, ge=-1, le=10)
    gross_margin_min: float = Field(default=0.50, ge=-1, le=1)
    volatility_max: float = Field(default=0.60, gt=0, le=10)


def _run_screening(body: ScreeningInput) -> dict:
    from v2.data import CachedFDClient
    from v2.screening import FilterConfig, TECH_30, run_screening

    tickers = _normalize_tickers(body.tickers or list(TECH_30))
    config = FilterConfig(
        market_cap_min=body.market_cap_min,
        market_cap_max=body.market_cap_max,
        revenue_growth_min=body.revenue_growth_min,
        gross_margin_min=body.gross_margin_min,
        volatility_max=body.volatility_max,
    )
    with CachedFDClient() as client:
        result = run_screening(tickers, client, config)
    return {"kind": "screening", "tickers": tickers, **result.model_dump()}


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
    _remember_run("backtest", result)
    return result


@router.post("/lab/event-study")
async def run_event_study(body: EventStudyInput) -> dict:
    result = await _lab_call(_run_event_study, body)
    _remember_run("event_study", result)
    return result


@router.post("/lab/screening")
async def run_screening(body: ScreeningInput) -> dict:
    result = await _lab_call(_run_screening, body)
    _remember_run("screening", result)
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
async def lab_runs() -> dict:
    return {"kind": "runs", "items": list(_LAB_RUNS)}
