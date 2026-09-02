"""Unit tests for the money-flow divergence pipeline.

Indicators are checked against hand-reasoned series; the detector is checked
against every branch of its truth table. No network, no LLM.
"""

from __future__ import annotations

import types

import pytest

from v2.moneyflow.detector import detect_divergence
from v2.moneyflow.indicators import (
    chaikin_money_flow,
    rsi,
    window_return,
)
from v2.moneyflow.models import DivergenceConfig

CFG = DivergenceConfig()


# --------------------------------------------------------------------------
# bar builders — CMF sign is controlled by where close sits in [low, high],
# independent of the close-to-close trend that drives RSI / price return.
# --------------------------------------------------------------------------

def _bar(close: float, flow: str):
    if flow == "inflow":          # close near the high → positive multiplier
        high, low = close + 0.1, close - 1.0
    elif flow == "outflow":       # close near the low → negative multiplier
        high, low = close + 1.0, close - 0.1
    else:                         # centered → ~0 multiplier
        high, low = close + 0.5, close - 0.5
    return types.SimpleNamespace(high=high, low=low, close=close, volume=1000)


def _bars(closes, flow):
    return [_bar(c, flow) for c in closes]


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------

def test_cmf_all_inflow_is_positive():
    closes = [100.0] * 20
    assert chaikin_money_flow(
        [c + 0.1 for c in closes], [c - 1.0 for c in closes],
        closes, [1000] * 20, window=20,
    ) > 0.5


def test_cmf_all_outflow_is_negative():
    closes = [100.0] * 20
    assert chaikin_money_flow(
        [c + 1.0 for c in closes], [c - 0.1 for c in closes],
        closes, [1000] * 20, window=20,
    ) < -0.5


def test_cmf_flat_bar_contributes_zero():
    # high == low on every bar → multiplier 0 → CMF 0
    assert chaikin_money_flow([100] * 20, [100] * 20, [100] * 20, [1000] * 20) == 0.0


def test_cmf_none_on_short_history():
    assert chaikin_money_flow([1, 2], [1, 2], [1, 2], [1, 1], window=20) is None


def test_rsi_monotonic_up_near_100():
    closes = [100 + i for i in range(30)]
    assert rsi(closes, 14) > 99


def test_rsi_flat_is_fifty():
    assert rsi([100.0] * 30, 14) == 50.0


def test_window_return():
    closes = [100.0] * 20 + [110.0]
    assert window_return(closes, 20) == pytest.approx(0.10)


# --------------------------------------------------------------------------
# detector truth table
# --------------------------------------------------------------------------

def test_accumulation_strong_flat_price_inflow_low_rsi():
    # 40 flat bars, then a mild 20-bar decline → 20d return ≈ -2% (flat band)
    # and a recent downtrend that drags RSI into the oversold/low zone.
    closes = [100.0] * 40 + [100.0 - 0.1 * i for i in range(1, 21)]
    sig = detect_divergence("AAA", _bars(closes, "inflow"), CFG)
    assert sig is not None
    assert sig.kind == "accumulation"
    assert sig.strength == "strong"
    assert sig.flow_state == "inflow"


def test_distribution_strong_flat_price_outflow_high_rsi():
    closes = [100.0] * 40 + [100.0 + 0.1 * i for i in range(1, 21)]
    sig = detect_divergence("BBB", _bars(closes, "outflow"), CFG)
    assert sig is not None
    assert sig.kind == "distribution"
    assert sig.strength == "strong"


def test_contradictory_accumulation_dropped():
    # Sustained uptrend (RSI pegged overbought) then a flat plateau: recent
    # price is flat but RSI is still hot AND hasn't risen over the window
    # (no bullish divergence) → accumulation branch hits the contradictory
    # cell → dropped. (A flat/down price with RSI *rising* would instead be
    # a legit bullish-divergence strong signal — that's covered elsewhere.)
    closes = [100.0 + i for i in range(40)] + [140.0] * 20
    sig = detect_divergence("CCC", _bars(closes, "inflow"), CFG)
    assert sig is None


def test_no_divergence_when_price_and_flow_agree():
    # strong uptrend + inflow → price rising with money → not a divergence.
    closes = [100.0 + 0.5 * i for i in range(60)]
    sig = detect_divergence("DDD", _bars(closes, "inflow"), CFG)
    assert sig is None


def test_none_on_insufficient_history():
    closes = [100.0] * 10
    assert detect_divergence("EEE", _bars(closes, "inflow"), CFG) is None
