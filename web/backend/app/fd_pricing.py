"""Per-request prices for financialdatasets.ai, used only to *estimate* spend.

Defaults are the published pay-as-you-go tiers as of the Lab build; the two
endpoints whose tier was not visible to us (financial-metrics, line-items)
default to the conservative $0.04. Override any of them with a JSON object in
``FD_PRICES``, e.g. ``FD_PRICES='{"financial_metrics":0.02}'``.
"""

from __future__ import annotations

import json
import os

DEFAULT_PRICES_USD: dict[str, float] = {
    "financial_metrics": 0.04,
    "line_items": 0.04,
    "prices": 0.02,
    "earnings": 0.01,
    "insider_trades": 0.04,
    "news": 0.04,
    "company_facts": 0.02,
}


def prices() -> dict[str, float]:
    table = dict(DEFAULT_PRICES_USD)
    raw = os.environ.get("FD_PRICES", "").strip()
    if raw:
        try:
            for k, v in json.loads(raw).items():
                if k in table:
                    table[k] = float(v)
        except (ValueError, TypeError, AttributeError):
            pass
    return table


def cost(counts: dict[str, int]) -> float:
    table = prices()
    return round(sum(table.get(k, 0.0) * n for k, n in counts.items()), 4)
