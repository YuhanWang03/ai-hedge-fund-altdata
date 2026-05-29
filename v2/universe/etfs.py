"""Sector ETF mappings — used for context-aware screening + anomaly detection.

Why this module: a "+5% volume spike" on NVDA means something very different
when SMH is +4.5% (board-wide rally — beta move) versus when SMH is -1%
(contrarian, ticker-specific signal — the kind that drives PMs to call up
sell-side desks). Tagging every signal with its sector-relative move
elevates real signal from market beta noise.
"""

from __future__ import annotations

# Sector / benchmark ETF symbols we track for relative-strength comparison.
# Pre-fetched ONCE per monitoring run by the orchestrator, then passed into
# every detect() call.
SECTOR_ETFS: list[str] = ["SPY", "XLK", "SMH"]

# Broad-market benchmark — used as a fallback for tickers without a sector
# mapping, and for "vs market" comparisons.
BENCHMARK_ETF: str = "SPY"

# TECH_30 ticker → primary sector ETF.
# Semiconductors get SMH; everything else gets XLK. Communications-services
# names (META/GOOGL/NFLX) are routed to XLK rather than XLC because the
# correlation is comparable and we want one universal "tech" benchmark.
TICKER_TO_SECTOR: dict[str, str] = {
    # Semiconductors → SMH (VanEck Semiconductor ETF)
    "NVDA": "SMH",
    "AMD":  "SMH",
    "AVGO": "SMH",
    "QCOM": "SMH",
    "INTC": "SMH",
    "TXN":  "SMH",
    "MU":   "SMH",

    # Mega-cap tech + software + internet → XLK (Tech Select Sector SPDR)
    "AAPL":  "XLK",
    "MSFT":  "XLK",
    "GOOGL": "XLK",
    "META":  "XLK",
    "AMZN":  "XLK",
    "TSLA":  "XLK",
    "ORCL":  "XLK",
    "CRM":   "XLK",
    "ADBE":  "XLK",
    "NOW":   "XLK",
    "SNOW":  "XLK",
    "PLTR":  "XLK",
    "CRWD":  "XLK",
    "PANW":  "XLK",
    "DDOG":  "XLK",
    "NFLX":  "XLK",
    "SHOP":  "XLK",
    "UBER":  "XLK",
    "IBM":   "XLK",
    "CSCO":  "XLK",
    "INTU":  "XLK",
    "ANET":  "XLK",
}


def sector_etf_for(ticker: str) -> str:
    """Return the primary sector ETF symbol for *ticker*. Defaults to SPY."""
    return TICKER_TO_SECTOR.get(ticker.upper(), BENCHMARK_ETF)
