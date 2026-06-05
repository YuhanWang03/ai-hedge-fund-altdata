"""Price source abstraction for daily OHLCV — Phase 4.5-mini.

Phase 1-4 routed every daily-price fetch through ``FDClient.get_prices``,
but FD's free tier carries a 1-3 day coverage lag. That forced
``v2.data_safety.fd_safe_today()`` to clamp the upper bound at
``today - 3``, which in turn made post-close cron cards display dates
that confused users ("2026-06-05 ⑨ Portfolio Risk" showing prices as
of 2026-06-02).

This module decouples the price-fetch surface from the rest of the FD
client. Callers depend on the :class:`PriceSource` Protocol; the
production default returns a :class:`YFinancePriceSource` (real-time
EOD, no lag). The :class:`FDPriceSource` is kept for backtest /
event-study where deterministic historical snapshots matter and the
3-day lag is acceptable.

Schema invariant:
    Both implementations return ``list[Price]`` sorted ascending by
    date, matching the existing :class:`v2.data.models.Price` shape so
    callers (screener / monitor / responder) need only swap the
    constructor — the loop body that reads ``p.close`` / ``p.volume``
    is unchanged.

Escape hatch:
    Set ``V2_PRICE_SOURCE=fd`` in env to force the FD path at runtime
    without code changes — for ops to flip back during a yfinance
    outage without redeploying.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from v2.data.models import Price


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class PriceSource(Protocol):
    """Daily OHLCV source. Returns ``list[Price]`` sorted ascending by date.

    ``start`` and ``end`` accept ISO date strings OR ``datetime.date``
    objects, mirroring ``FDClient.get_prices``'s tolerant input shape.
    Implementations that fail (network error, rate limit, empty data)
    return ``[]`` and log a warning — never raise. Callers downstream
    already handle the empty case (Stage 0 audit confirmed all 5
    in-scope callers gate on ``if not prices`` or ``len(prices) < N``).
    """

    def get_prices(self, ticker: str, start: Any, end: Any) -> list[Price]: ...


# ---------------------------------------------------------------------------
# FD path (legacy / fallback)
# ---------------------------------------------------------------------------

class FDPriceSource:
    """Thin adapter around ``FDClient.get_prices``. Used by backtest +
    event-study (need reproducible historical snapshots) and as the
    runtime escape hatch when ``V2_PRICE_SOURCE=fd``.

    Constructor signature accepts any object with a ``.get_prices``
    method so callers can wire :class:`v2.data.CachedFDClient` or the
    raw :class:`v2.data.FDClient` interchangeably.
    """

    def __init__(self, fd_client) -> None:
        self._fd = fd_client

    def get_prices(self, ticker: str, start: Any, end: Any) -> list[Price]:
        return self._fd.get_prices(ticker, start, end)


# ---------------------------------------------------------------------------
# yfinance path (default)
# ---------------------------------------------------------------------------

class YFinancePriceSource:
    """Real-time EOD daily prices via yfinance.

    Maps yfinance's pandas DataFrame output into the FD-shaped
    ``Price`` dataclass so downstream loops can stay untouched.

    ``yfinance.Ticker(ticker).history(start, end)`` is END-EXCLUSIVE
    on the ``end`` arg, so we add 1 day to the requested upper bound to
    get inclusive semantics (matches FD's behavior — Stage 0 audit
    confirmed all callers expect end to land in the returned series
    when data exists).

    Failure modes — all return ``[]`` + WARNING log:
    - Network error / 429 rate limit
    - Empty DataFrame (ticker not on yfinance, e.g. delisted)
    - Schema mismatch (Open/High/Low/Close/Volume missing)
    """

    def __init__(self, ticker_factory=None) -> None:
        """Args:
            ticker_factory: optional callable ``(symbol) -> Ticker-like``
                used as a test seam. Default uses ``yfinance.Ticker``
                lazily so the import only fires when the source is
                actually instantiated (lets sandbox tests stub yfinance
                via sys.modules without forcing it as a hard dep at
                module load).
        """
        self._ticker_factory = ticker_factory

    def _make_ticker(self, symbol: str):
        if self._ticker_factory is not None:
            return self._ticker_factory(symbol)
        import yfinance as yf
        return yf.Ticker(symbol)

    @staticmethod
    def _to_iso(d: Any) -> str:
        """Accept str OR date OR datetime, return ISO date string."""
        if isinstance(d, str):
            return d
        if isinstance(d, datetime):
            return d.date().isoformat()
        if isinstance(d, date):
            return d.isoformat()
        return str(d)

    @staticmethod
    def _bump_end(end_iso: str) -> str:
        """yfinance `end` is exclusive; bump +1 day for inclusive semantics."""
        try:
            return (date.fromisoformat(end_iso) + timedelta(days=1)).isoformat()
        except ValueError:
            return end_iso

    def get_prices(self, ticker: str, start: Any, end: Any) -> list[Price]:
        start_iso = self._to_iso(start)
        end_iso = self._bump_end(self._to_iso(end))

        try:
            ticker_obj = self._make_ticker(ticker)
            df = ticker_obj.history(
                start=start_iso, end=end_iso, auto_adjust=False,
            )
        except Exception as exc:                       # noqa: BLE001
            logger.warning(
                "yfinance get_prices(%s, %s, %s) failed: %s",
                ticker, start_iso, end_iso, exc,
            )
            return []

        if df is None or len(df) == 0:
            logger.warning(
                "yfinance returned empty for %s [%s, %s]",
                ticker, start_iso, end_iso,
            )
            return []

        prices: list[Price] = []
        for idx, row in df.iterrows():
            try:
                row_date = idx.date() if hasattr(idx, "date") else idx
                prices.append(Price(
                    date=row_date,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]) if not _is_nan(row["Volume"]) else 0,
                ))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "yfinance row decode failed for %s at %s: %s",
                    ticker, idx, exc,
                )
                continue

        return sorted(prices, key=lambda p: p.date)


def _is_nan(v) -> bool:
    """NaN-safe check that survives strings and ints."""
    try:
        import math
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def default_price_source() -> PriceSource:
    """Production default — :class:`YFinancePriceSource`.

    Set env var ``V2_PRICE_SOURCE=fd`` to force the FD path (escape
    hatch for ops during a yfinance outage). The FD path wraps a
    freshly-constructed :class:`v2.data.CachedFDClient`; callers
    that want to inject a specific FD instance should construct
    :class:`FDPriceSource` directly.
    """
    if os.environ.get("V2_PRICE_SOURCE", "").strip().lower() == "fd":
        from v2.data import CachedFDClient
        return FDPriceSource(CachedFDClient())
    return YFinancePriceSource()


__all__ = [
    "PriceSource",
    "FDPriceSource",
    "YFinancePriceSource",
    "default_price_source",
]
