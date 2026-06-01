"""End-to-end orchestration for lateral expansion (玩法 ③).

Flow:
    1. LLM discovers raw neighbor candidates for each seed (4 categories).
    2. Aggregate by ticker — same ticker may appear under multiple seeds/categories.
    3. Verify each unique ticker exists via FD company_facts.
    4. For real + new tickers, build a ScreenCandidate and run the screening filter.
    5. For filter-passers, generate bull/bear narration (reusing screening.narrate).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from v2.data.client import FDClient
from v2.data.yfinance_client import KNOWN_ADRS, YFinanceClient
from v2.lateral.discover import discover
from v2.lateral.models import LateralResult, Neighbor
from v2.lateral.verify import verify, verify_relation
from v2.screening import (
    FilterConfig,
    ScreenCandidate,
    build_candidate,
    narrate,
    passes_filter,
)

logger = logging.getLogger(__name__)


def run_lateral_expansion(
    seeds: list[str],
    universe: set[str],
    fd_client: FDClient,
    filter_config: FilterConfig,
) -> LateralResult:
    """Run the full pipeline for one expansion pass."""
    # fd_safe_today: respect FD's 1-3 day coverage lag.
    from v2.data_safety import fd_safe_today
    today = fd_safe_today()
    today_str = today.isoformat()
    history_start = (today - timedelta(days=400)).isoformat()

    # Step 1: LLM discovery
    logger.info("Discovering neighbors for %d seeds...", len(seeds))
    pairs, tokens = discover(seeds)

    # Step 2: Aggregate by ticker — collect all (seed, category) labels
    by_ticker: dict[str, Neighbor] = {}
    for ticker, label in pairs:
        if ticker not in by_ticker:
            by_ticker[ticker] = Neighbor(ticker=ticker)
        by_ticker[ticker].labels.append(label)
    neighbors = list(by_ticker.values())
    logger.info("Aggregated %d unique tickers from %d raw suggestions",
                len(neighbors), len(pairs))

    # Step 3: Verify existence
    api_calls = 0
    for n in neighbors:
        api_calls += verify(n, fd_client, universe)

    # Step 3.5 (Resume polish): Tavily-verify the seed-neighbor relationship.
    # Only for tickers we confirmed exist and aren't already in our universe.
    # Each unique neighbor costs ≤ 1 Tavily call.
    tavily_calls = 0
    for n in neighbors:
        if n.exists and not n.already_in_universe:
            tavily_calls += verify_relation(n)
    logger.info("Tavily relation checks: %d calls, %d verified",
                tavily_calls,
                sum(1 for n in neighbors if n.relation_verified))

    # Step 4: Hard-filter the real + new tickers
    new_real = [n for n in neighbors if n.exists and not n.already_in_universe]
    logger.info("%d new real candidates to screen", len(new_real))

    yf_client = YFinanceClient()  # used as fallback for foreign ADRs

    for n in new_real:
        use_fallback = yf_client if n.ticker in KNOWN_ADRS else None
        candidate = build_candidate(
            n.ticker, fd_client, today_str, history_start,
            fallback=use_fallback,
        )
        api_calls += 2  # metrics + prices
        if candidate is None:
            n.failed_reason = "数据不足"
            continue
        n.candidate = candidate
        if passes_filter(candidate, filter_config):
            n.passed_filter = True
        else:
            n.failed_reason = _explain_failure(candidate, filter_config)

    # Step 5: Narrate passers (reuse screening narrator)
    passers = [n for n in new_real if n.passed_filter and n.candidate is not None]
    if passers:
        logger.info("Narrating %d passers with DeepSeek...", len(passers))
        narrations, narr_tokens = narrate([n.candidate for n in passers])
        tokens += narr_tokens
        for n in passers:
            note = narrations.get(n.ticker, {})
            n.bull = note.get("bull", "") or ""
            n.bear = note.get("bear", "") or ""

    return LateralResult(
        date=today_str,
        seeds=seeds,
        neighbors=neighbors,
        llm_tokens=tokens,
        api_calls=api_calls,
        tavily_calls=tavily_calls,
    )


def _explain_failure(c: ScreenCandidate, cfg: FilterConfig) -> str:
    """Pinpoint which filter rule the candidate failed (first failure wins)."""
    if c.market_cap is None:
        return "市值数据缺失"
    if c.market_cap < cfg.market_cap_min:
        return f"市值 ${c.market_cap / 1e9:.1f}B < ${cfg.market_cap_min / 1e9:.0f}B"
    if c.market_cap > cfg.market_cap_max:
        return f"市值 ${c.market_cap / 1e12:.1f}T > ${cfg.market_cap_max / 1e12:.0f}T"
    if c.revenue_growth is None:
        return "营收数据缺失"
    if c.revenue_growth < cfg.revenue_growth_min:
        return f"营收 {c.revenue_growth:+.1%} < {cfg.revenue_growth_min:.1%}"
    if c.gross_margin is None:
        return "毛利数据缺失"
    if c.gross_margin < cfg.gross_margin_min:
        return f"毛利 {c.gross_margin:.1%} < {cfg.gross_margin_min:.1%}"
    if c.volatility is None:
        return "波动率数据缺失"
    if c.volatility > cfg.volatility_max:
        return f"波动 {c.volatility:.1%} > {cfg.volatility_max:.1%}"
    return "未通过"
