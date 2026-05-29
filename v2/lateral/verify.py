"""Verify that LLM-suggested neighbors are (a) real tickers and (b) actually
have the relationship the LLM claims (Step 3 of resume polish)."""

from __future__ import annotations

import logging
import os

from tavily import TavilyClient

from v2.data.client import FDClient
from v2.lateral.models import Neighbor

logger = logging.getLogger(__name__)


# Search-query keywords per relation category (chosen for max recall on Tavily)
_RELATION_KEYWORDS = {
    "supplier":     "supplier supply chain",
    "customer":     "customer client",
    "smaller_peer": "competitor peer",
    "beneficiary":  "partnership beneficiary",
}


def verify(neighbor: Neighbor, fd: FDClient, universe: set[str]) -> int:
    """Set exists/sector/already_in_universe on *neighbor*. Returns API calls used."""
    if neighbor.ticker in universe:
        neighbor.exists = True
        neighbor.already_in_universe = True
        return 0

    facts = fd.get_company_facts(neighbor.ticker)
    if facts is None:
        neighbor.exists = False
        return 1

    neighbor.exists = True
    neighbor.sector = facts.sector
    return 1


# ---------------------------------------------------------------------------
# Step 3: Tavily relation verification
# ---------------------------------------------------------------------------


def verify_relation(neighbor: Neighbor) -> int:
    """Search Tavily for evidence of the seed-neighbor relationship.

    Iterates through neighbor.labels (each is a seed/category pair). Stops at
    the first label whose relationship is confirmed. Sets relation_verified
    and relation_evidence_url in-place.

    Returns the number of Tavily calls actually made (≥ 0).
    """
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key or not neighbor.labels:
        return 0

    neighbor.relation_checked = True
    client = TavilyClient(api_key=api_key)
    calls = 0

    for label in neighbor.labels:
        keywords = _RELATION_KEYWORDS.get(label.category, "")
        query = f"{label.seed} {neighbor.ticker} {keywords}".strip()

        try:
            response = client.search(
                query=query,
                max_results=3,
                topic="general",
                days=365,
                search_depth="basic",
            )
        except Exception as exc:
            logger.warning("Tavily relation search failed for %s/%s: %s",
                           label.seed, neighbor.ticker, exc)
            continue

        calls += 1
        results = response.get("results", []) if response else []

        # Confirm: at least one result text mentions BOTH tickers
        seed_up = label.seed.upper()
        nb_up = neighbor.ticker.upper()
        for r in results:
            text = ((r.get("title") or "") + " " +
                    (r.get("content") or "")).upper()
            if seed_up in text and nb_up in text:
                neighbor.relation_verified = True
                neighbor.relation_evidence_url = (r.get("url") or "")[:200]
                return calls       # first hit is enough

    return calls
