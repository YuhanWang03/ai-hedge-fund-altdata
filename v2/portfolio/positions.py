"""Flatten Alpaca's portfolio snapshot into ``PositionFlat`` list.

The Alpaca client returns positions with USD-typed strings and a sector
attribution that doesn't exist in the SDK at all. This module:

1. Pulls ``get_portfolio()`` (existing wrapper in v2/broker)
2. Computes each position's share of ``portfolio_value``
3. Maps each ticker to its sector ETF bucket via ``v2.universe``

Soft failure: Alpaca unavailable → empty list + a warning string. The
upstream pipeline aggregates these warnings into ``RiskReport.warnings``
so the cron card can say "组合数据暂不可用" instead of crashing.
"""

from __future__ import annotations

import logging
from typing import Any

from v2.portfolio.models import PositionFlat
from v2.universe import sector_bucket_for

logger = logging.getLogger(__name__)


def get_flat_positions(broker: Any | None = None) -> tuple[list[PositionFlat], float, float, list[str]]:
    """Pull Alpaca positions and flatten them into ``PositionFlat`` rows.

    Args:
        broker: optional module shim with ``get_portfolio()`` — defaults
            to :mod:`v2.broker`. Pass a mock with the same interface for
            unit tests.

    Returns:
        ``(positions, portfolio_value, cash, warnings)`` where ``positions``
        is sorted by weight descending. ``portfolio_value`` and ``cash`` are
        the account-level dollar amounts (``portfolio_value`` is invested
        equity, NOT total equity including cash). On Alpaca failure all
        values are zero / empty and warnings carries the reason.
    """
    if broker is None:
        from v2 import broker as _broker
        broker = _broker

    warnings: list[str] = []

    try:
        snap = broker.get_portfolio()
    except Exception as exc:
        # Includes AlpacaUnavailable + transient network errors.
        msg = f"Alpaca 持仓数据暂不可用：{exc}"
        logger.warning(msg)
        warnings.append(msg)
        return [], 0.0, 0.0, warnings

    account = snap.get("account") or {}
    positions_raw = snap.get("positions") or []

    portfolio_value = float(account.get("portfolio_value") or 0.0)
    cash = float(account.get("cash") or 0.0)

    if portfolio_value <= 0 and not positions_raw:
        # Brand-new account, no positions yet.
        return [], portfolio_value, cash, warnings

    flats: list[PositionFlat] = []
    for raw in positions_raw:
        try:
            ticker = str(raw.get("symbol") or "").upper().strip()
            mv = float(raw.get("market_value") or 0.0)
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping malformed position %r: %s", raw, exc)
            continue
        if not ticker:
            continue
        weight = (mv / portfolio_value) if portfolio_value > 0 else 0.0
        flats.append(PositionFlat(
            ticker=ticker,
            market_value=mv,
            weight=weight,
            sector_etf=sector_bucket_for(ticker),
        ))

    # Largest weights first — every downstream consumer wants this order.
    flats.sort(key=lambda p: p.weight, reverse=True)
    return flats, portfolio_value, cash, warnings
