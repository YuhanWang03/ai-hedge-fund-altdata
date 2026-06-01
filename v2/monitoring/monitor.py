"""Monitoring orchestration: universe -> prices -> detect -> list of anomalies."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from v2.data.client import FDClient
from v2.monitoring.detectors import detect
from v2.monitoring.models import Anomaly, MonitorConfig
from v2.monitoring.options import OptionsTracker
from v2.universe import SECTOR_ETFS

logger = logging.getLogger(__name__)

# 270 calendar days ≈ 252 trading days + buffer for weekends/holidays
_HISTORY_CALENDAR_DAYS = 380


def run_monitoring(
    tickers: list[str],
    fd_client: FDClient,
    config: MonitorConfig,
) -> list[Anomaly]:
    """Scan *tickers*, return all anomalies found on the latest trading day."""
    # fd_safe_today caps end at today - 3 days so the FD request stays
    # inside the coverage window (no HTTP 400 → empty cascade).
    from v2.data_safety import fd_safe_today
    end = fd_safe_today()
    start = (end - timedelta(days=_HISTORY_CALENDAR_DAYS)).isoformat()
    end_str = end.isoformat()

    # Shared OptionsTracker — one DB connection lifetime across all tickers
    options_tracker = OptionsTracker()

    # Pre-fetch sector ETF price series ONCE per run. Bounded extra cost:
    # |SECTOR_ETFS| FD calls regardless of universe size.
    etf_prices = {}
    for etf in SECTOR_ETFS:
        try:
            series = fd_client.get_prices(etf, start, end_str)
        except Exception as exc:
            logger.warning("ETF %s prefetch failed: %s", etf, exc)
            continue
        if series:
            etf_prices[etf] = series

    anomalies: list[Anomaly] = []
    for ticker in tickers:
        prices = fd_client.get_prices(ticker, start, end_str)
        if not prices:
            continue
        anomaly = detect(
            ticker, prices, config,
            fd_client=fd_client,
            options_tracker=options_tracker,
            etf_prices=etf_prices,
        )
        if anomaly is not None:
            anomalies.append(anomaly)
            logger.info("Anomaly: %s %s", ticker, anomaly.flags)

    return anomalies
