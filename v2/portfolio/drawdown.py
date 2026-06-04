"""Peak-trough drawdown over the 1-month window.

Two scalars:

- ``current_drawdown_pct`` = (current - running_max_so_far) / running_max_so_far.
  Always ≤ 0. If we're at a new high, drawdown is 0.
- ``max_drawdown_pct`` = worst (trough - peak) / peak observed across the
  window. The classic "peak-trough" measure.

Edge cases:
- Single-point history → both fields = 0.0 (not None) so the card can
  still render "回撤 0.0%" instead of dropping the row. Peak == current,
  no trough yet.
- Empty equity series → ``DrawdownMetrics.unavailable()`` (all None).
- Alpaca down → warnings collected, unavailable() returned.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from v2.portfolio.models import DrawdownMetrics

logger = logging.getLogger(__name__)


def compute_drawdown(broker: Any | None = None) -> tuple[DrawdownMetrics, list[str]]:
    """Compute current + max drawdown over 1M of daily equity history.

    Returns ``(metrics, warnings)``. On any Alpaca failure → unavailable()
    metric + a warning string explaining why.
    """
    if broker is None:
        from v2 import broker as _broker
        broker = _broker

    warnings: list[str] = []

    try:
        hist = broker.get_portfolio_history(period="1M", timeframe="1D")
    except Exception as exc:
        msg = f"Alpaca 历史 equity 不可用：{exc}"
        logger.warning(msg)
        warnings.append(msg)
        return DrawdownMetrics.unavailable(), warnings

    equity = hist.get("equity") or []
    timestamps = hist.get("timestamp") or []

    if not equity:
        # Empty series. Caller renders "暂无回撤数据".
        return DrawdownMetrics.unavailable(), warnings

    # Single point: peak == current, no drawdown observable yet.
    if len(equity) == 1:
        peak_value = equity[0]
        peak_date = _ts_to_date(timestamps[0]) if timestamps else None
        return DrawdownMetrics(
            current_drawdown_pct=0.0,
            max_drawdown_pct=0.0,
            peak_value=peak_value,
            peak_date=peak_date,
        ), warnings

    # Walk the series tracking running peak + worst drawdown ever seen.
    running_peak = equity[0]
    running_peak_idx = 0
    max_dd = 0.0
    max_dd_peak_idx = 0   # the peak that produced max_dd

    for i, val in enumerate(equity):
        if val > running_peak:
            running_peak = val
            running_peak_idx = i
            continue
        dd = (val - running_peak) / running_peak if running_peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
            max_dd_peak_idx = running_peak_idx

    current = equity[-1]
    current_dd = (current - running_peak) / running_peak if running_peak > 0 else 0.0

    # The peak attributed to the user is the one that produced max_dd —
    # that's the "from this peak, you fell N%" story the card tells.
    # If max_dd == 0 (still at all-time highs), report the latest peak.
    peak_idx = max_dd_peak_idx if max_dd < 0 else running_peak_idx
    peak_value = equity[peak_idx]
    peak_date = _ts_to_date(timestamps[peak_idx]) if peak_idx < len(timestamps) else None

    return DrawdownMetrics(
        current_drawdown_pct=current_dd,
        max_drawdown_pct=max_dd,
        peak_value=peak_value,
        peak_date=peak_date,
    ), warnings


def _ts_to_date(ts: int | float) -> date | None:
    """Alpaca history timestamps are unix seconds. Convert to ISO date."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None
