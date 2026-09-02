"""GET /api/portfolio — structured account + P&L + positions (+ equity curve).

Calls the v2 broker layer directly (not the HTML responder) so the frontend
gets clean JSON to render its own cards. Read-only; Alpaca paper by default.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.auth import require_owner

router = APIRouter(prefix="/api", tags=["portfolio"])


def _fetch() -> dict:
    from v2.broker.alpaca_client import (
        get_pnl,
        get_portfolio,
        get_portfolio_history,
    )

    pf = get_portfolio()
    pnl = get_pnl()
    # Equity curve is a nice-to-have — never fail the whole panel over it.
    try:
        hist = get_portfolio_history(period="1M", timeframe="1D")
        history = {"timestamp": hist["timestamp"], "equity": hist["equity"]}
    except Exception:
        history = {"timestamp": [], "equity": []}

    return {
        "account": pf["account"],
        "positions": pf["positions"],
        "pnl": pnl,
        "history": history,
    }


@router.get("/portfolio", dependencies=[Depends(require_owner)])
async def portfolio() -> dict:
    try:
        return await run_in_threadpool(_fetch)
    except Exception as exc:
        # Missing Alpaca creds or API error → 503 with the reason (the
        # frontend shows it inline instead of a blank panel).
        raise HTTPException(status_code=503, detail=str(exc))
