"""Daily ARK ETF holdings snapshot — runs after US market close.

For each ARK fund, fetches the latest CSV and saves to data/etf.db.
Builds a cumulative time series that the bot's /etf command uses to
compute 24h rebalances.

Telegram-quiet by design: pure data ingestion. Significant-rebalance
alerts could be layered on top later.

Triggered by the scheduler Mon-Fri 17:00 ET — ARK posts daily files
around 16:00 ET, giving us an hour of buffer.
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from v2.etf import SUPPORTED_FUNDS, fetch_holdings, save_snapshot
from v2.reporting import notify_on_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@notify_on_error("etf-daily")
def main() -> int:
    load_dotenv()

    total_funds = 0
    total_rows = 0

    for symbol in SUPPORTED_FUNDS:
        logger.info("Fetching %s ...", symbol)
        try:
            holdings, snap_date = fetch_holdings(symbol)
        except Exception as exc:
            logger.warning("  %s fetch failed: %s", symbol, exc)
            continue

        if not holdings:
            logger.warning("  %s returned no holdings", symbol)
            continue

        try:
            n = save_snapshot(symbol, snap_date, holdings)
            logger.info("  %s · %s · %d rows saved", symbol, snap_date, n)
            total_funds += 1
            total_rows += n
        except Exception as exc:
            logger.warning("  %s save failed: %s", symbol, exc)
            continue

    logger.info(
        "Done. %d funds · %d position-rows saved.",
        total_funds, total_rows,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
