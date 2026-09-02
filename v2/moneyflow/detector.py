"""Deterministic three-axis divergence detector.

Given a ticker's daily OHLCV bars, classify each of three orthogonal axes —
price trend, money flow (CMF), momentum (RSI) — and combine them into an
accumulation / distribution verdict via a fixed truth table. No LLM here:
the verdict and every number are pure Python so the trace is auditable.

Truth table (price / flow → kind, RSI → strength):

  price flat|down + inflow  → accumulation
      RSI oversold|low OR bullish-divergence → strong
      RSI neutral                            → moderate
      RSI high|overbought                    → DROP (contradictory)

  price flat|up + outflow   → distribution
      RSI high|overbought OR bearish-divergence → strong
      RSI neutral                               → moderate
      RSI oversold|low                          → DROP (likely washout)

Anything else → no divergence (return None).
"""

from __future__ import annotations

from collections.abc import Sequence

from v2.moneyflow.indicators import chaikin_money_flow, rsi_series, window_return
from v2.moneyflow.models import DivergenceConfig, MoneyFlowSignal


def _rsi_zone(value: float, cfg: DivergenceConfig) -> str:
    if value <= cfg.rsi_oversold:
        return "oversold"
    if value < cfg.rsi_low:
        return "low"
    if value <= cfg.rsi_high:
        return "neutral"
    if value < cfg.rsi_overbought:
        return "high"
    return "overbought"


def _rsi_divergence(closes: Sequence[float], cfg: DivergenceConfig) -> str:
    """Compare price move vs RSI move over price_window.

    bullish = price flat/down but RSI rose (hidden strength).
    bearish = price flat/up but RSI fell (hidden weakness).
    """
    series = rsi_series(closes, cfg.rsi_window)
    w = cfg.price_window
    if len(series) < w + 1:
        return "none"
    rsi_now, rsi_prev = series[-1], series[-1 - w]
    price_now, price_prev = float(closes[-1]), float(closes[-1 - w])
    if price_prev <= 0:
        return "none"
    price_ret = (price_now - price_prev) / price_prev
    rsi_delta = rsi_now - rsi_prev
    if price_ret <= 0 and rsi_delta >= cfg.rsi_divergence_delta:
        return "bullish"
    if price_ret >= 0 and rsi_delta <= -cfg.rsi_divergence_delta:
        return "bearish"
    return "none"


def detect_divergence(
    ticker: str,
    prices,
    cfg: DivergenceConfig,
) -> MoneyFlowSignal | None:
    """Return a MoneyFlowSignal, or None when there's no clean divergence.

    ``prices`` is a chronological list of OHLCV bars (objects with
    .high/.low/.close/.volume — e.g. v2.data.models.Price).
    """
    if len(prices) < cfg.min_history:
        return None

    highs = [p.high for p in prices]
    lows = [p.low for p in prices]
    closes = [p.close for p in prices]
    volumes = [p.volume for p in prices]

    cmf = chaikin_money_flow(highs, lows, closes, volumes, cfg.cmf_window)
    price_ret = window_return(closes, cfg.price_window)
    series = rsi_series(closes, cfg.rsi_window)
    if cmf is None or price_ret is None or not series:
        return None
    rsi_val = series[-1]

    # ---- classify the three axes ----
    if price_ret > cfg.flat_band:
        price_state = "up"
    elif price_ret < -cfg.flat_band:
        price_state = "down"
    else:
        price_state = "flat"

    if cmf >= cfg.cmf_inflow_threshold:
        flow_state = "inflow"
    elif cmf <= cfg.cmf_outflow_threshold:
        flow_state = "outflow"
    else:
        flow_state = "neutral"

    rsi_zone = _rsi_zone(rsi_val, cfg)
    divergence = _rsi_divergence(closes, cfg)

    # ---- combine via the truth table ----
    if flow_state == "inflow" and price_state in ("flat", "down"):
        kind = "accumulation"
        if rsi_zone in ("oversold", "low") or divergence == "bullish":
            strength = "strong"
        elif rsi_zone == "neutral":
            strength = "moderate"
        else:  # high / overbought — price weak but momentum hot: contradictory
            return None
    elif flow_state == "outflow" and price_state in ("flat", "up"):
        kind = "distribution"
        if rsi_zone in ("high", "overbought") or divergence == "bearish":
            strength = "strong"
        elif rsi_zone == "neutral":
            strength = "moderate"
        else:  # oversold / low — more likely a washout than distribution
            return None
    else:
        return None

    return MoneyFlowSignal(
        ticker=ticker,
        price=float(closes[-1]),
        price_return=float(price_ret),
        price_state=price_state,
        cmf=float(cmf),
        flow_state=flow_state,
        rsi=float(rsi_val),
        rsi_zone=rsi_zone,
        rsi_divergence=divergence,
        kind=kind,
        strength=strength,
    )
