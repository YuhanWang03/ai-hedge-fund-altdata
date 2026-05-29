"""Alpaca paper-trading account adapter.

Read-only: portfolio + P&L only. We deliberately do NOT expose order
placement here — this is a research / monitoring system, not a trading bot.
If we ever want to trade from Telegram, that goes in a separate
explicitly-named module behind an extra confirmation layer.

API keys come from env: APCA_API_KEY_ID + APCA_API_SECRET_KEY (Alpaca's
standard names). PAPER endpoint is the default; flip APCA_PAPER=false to
hit live.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date

from alpaca.trading.client import TradingClient

logger = logging.getLogger(__name__)


class AlpacaUnavailable(RuntimeError):
    """Raised when Alpaca credentials are missing or the API is unreachable."""


@dataclass
class AlpacaConfig:
    api_key: str
    secret_key: str
    paper: bool = True


def _load_config() -> AlpacaConfig:
    api_key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not api_key or not secret:
        raise AlpacaUnavailable(
            "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set — "
            "add them to .env to enable /portfolio + /pnl."
        )
    paper_flag = os.environ.get("APCA_PAPER", "true").strip().lower()
    return AlpacaConfig(
        api_key=api_key,
        secret_key=secret,
        paper=paper_flag not in ("false", "0", "no"),
    )


def _client() -> TradingClient:
    cfg = _load_config()
    return TradingClient(
        api_key=cfg.api_key,
        secret_key=cfg.secret_key,
        paper=cfg.paper,
    )


def get_portfolio() -> dict:
    """Return current account + positions snapshot.

    Schema:
        account:
            cash, portfolio_value, buying_power, status, paper (bool)
        positions: list of:
            symbol, qty, avg_entry_price, current_price, market_value,
            unrealized_pl, unrealized_pl_pct, side
    """
    try:
        tc = _client()
        account = tc.get_account()
        positions = tc.get_all_positions()
    except AlpacaUnavailable:
        raise
    except Exception as exc:
        logger.exception("Alpaca portfolio fetch failed")
        raise AlpacaUnavailable(f"Alpaca API error: {exc}") from exc

    pos_list: list[dict] = []
    for p in positions:
        try:
            mv = float(p.market_value or 0)
            cost = float(p.cost_basis or 0)
            upl = float(p.unrealized_pl or 0)
            upl_pct = (upl / cost) if cost > 0 else 0.0
            pos_list.append({
                "symbol": str(p.symbol or ""),
                "qty": float(p.qty or 0),
                "avg_entry_price": float(p.avg_entry_price or 0),
                "current_price": float(p.current_price or 0),
                "market_value": mv,
                "unrealized_pl": upl,
                "unrealized_pl_pct": upl_pct,
                "side": str(getattr(p, "side", "long")).lower(),
            })
        except (ValueError, AttributeError) as exc:
            logger.warning("Skipping malformed position: %s", exc)
            continue

    pos_list.sort(key=lambda x: abs(x["market_value"]), reverse=True)

    return {
        "account": {
            "cash":            float(account.cash or 0),
            "portfolio_value": float(account.portfolio_value or 0),
            "buying_power":    float(account.buying_power or 0),
            "status":          _strip_enum(account.status),
            "paper":           _load_config().paper,
        },
        "positions": pos_list,
    }


def _strip_enum(value) -> str:
    """Alpaca enums repr as 'AccountStatus.ACTIVE' — return just 'active'."""
    s = str(value or "")
    if "." in s:
        s = s.split(".", 1)[1]
    return s.lower()


def get_pnl() -> dict:
    """Return today's intraday P&L and broader equity stats.

    Schema:
        date, paper,
        equity, last_equity, intraday_pl, intraday_pl_pct,
        cash, portfolio_value, buying_power,
        position_count, long_value, short_value
    """
    try:
        tc = _client()
        account = tc.get_account()
        positions = tc.get_all_positions()
    except AlpacaUnavailable:
        raise
    except Exception as exc:
        logger.exception("Alpaca PnL fetch failed")
        raise AlpacaUnavailable(f"Alpaca API error: {exc}") from exc

    equity = float(account.equity or 0)
    last_equity = float(account.last_equity or 0)
    intraday_pl = equity - last_equity
    intraday_pct = (intraday_pl / last_equity) if last_equity > 0 else 0.0

    long_value = 0.0
    short_value = 0.0
    for p in positions:
        try:
            mv = float(p.market_value or 0)
        except (ValueError, TypeError):
            continue
        if mv >= 0:
            long_value += mv
        else:
            short_value += mv

    return {
        "date":             date.today().isoformat(),
        "paper":            _load_config().paper,
        "equity":           equity,
        "last_equity":      last_equity,
        "intraday_pl":      intraday_pl,
        "intraday_pl_pct":  intraday_pct,
        "cash":             float(account.cash or 0),
        "portfolio_value":  float(account.portfolio_value or 0),
        "buying_power":     float(account.buying_power or 0),
        "position_count":   len(positions),
        "long_value":       long_value,
        "short_value":      short_value,
    }
