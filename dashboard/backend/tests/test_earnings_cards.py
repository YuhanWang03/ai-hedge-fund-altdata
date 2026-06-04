"""Unit tests for v2.earnings._bot_cards.

Covers the rendering of /earnings TICKER (single-ticker card) and
/earnings (no-arg) calendar paths. Pure functions — no network, no FD.

The wrapper responder (v2.bot.responders.earnings_view / earnings_calendar)
needs the production v2.data module and is exercised manually by the
operator on the deploy box; the unit-test coverage here pins the
formatting + filtering semantics that survive past Stage 5's move into
v2/reporting/formatters.py.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# Make v2/ importable when pytest runs from the dashboard backend dir.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.earnings._bot_cards import (   # noqa: E402
    format_earnings_calendar,
    format_earnings_view,
)
from v2.earnings.models import EarningsEvent, EarningsHistorical   # noqa: E402


_TODAY = date.today().isoformat()


def _future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Single-ticker card
# ---------------------------------------------------------------------------

def test_earnings_view_renders_with_next_and_last():
    next_ev = EarningsEvent(
        ticker="AAPL", release_date=_future(58),
        when="amc", eps_estimate=1.51, revenue_estimate=94.0e9,
    )
    last_ev = EarningsHistorical(
        ticker="AAPL", report_period="2026-Q2",
        filing_date="2026-04-30", source_type="8-K",
        eps_actual=1.42, eps_estimate=1.40,
        eps_surprise="BEAT",
        revenue_actual=89.5e9, revenue_estimate=88.2e9,
    )
    out = format_earnings_view(
        "AAPL", next_event=next_ev, last_event=last_ev,
        is_held=True, is_watchlist=False, today_iso=_TODAY,
    )
    assert "AAPL 财报" in out
    assert "D-58" in out
    assert "盘后" in out
    assert "$1.51" in out
    assert "$94.00B" in out
    assert "⭐持仓" in out
    assert "BEAT" in out
    assert "上次 (2026-Q2)" in out
    assert "$1.42" in out and "$1.40" in out
    # Both eps and revenue surprise should render with +%
    assert "+1.5%" in out
    assert "+1.4%" in out


def test_earnings_view_handles_no_upcoming():
    last_ev = EarningsHistorical(
        ticker="MSFT", report_period="2026-Q1",
        filing_date="2026-01-28", source_type="8-K",
        eps_actual=3.10, eps_estimate=3.00,
        eps_surprise="BEAT",
    )
    out = format_earnings_view(
        "MSFT", next_event=None, last_event=last_ev,
        is_held=False, is_watchlist=True, today_iso=_TODAY,
    )
    assert "暂未发现即将的财报安排" in out
    assert "上次 (2026-Q1)" in out
    assert "⭐watchlist" in out
    # Held badge must NOT appear when only watchlist is true.
    assert "⭐持仓" not in out


def test_earnings_view_handles_no_historical():
    next_ev = EarningsEvent(
        ticker="NVDA", release_date=_future(3),
        when="amc", eps_estimate=0.75,
    )
    out = format_earnings_view(
        "NVDA", next_event=next_ev, last_event=None,
        is_held=False, is_watchlist=False, today_iso=_TODAY,
    )
    assert "D-3" in out
    assert "暂无最近财报数据" in out
    # No badge when neither held nor watchlist.
    assert "⭐" not in out


def test_earnings_view_d0_label():
    """Release today renders as '今日', not D-0."""
    next_ev = EarningsEvent(
        ticker="TSLA", release_date=_TODAY, when="amc",
    )
    out = format_earnings_view(
        "TSLA", next_event=next_ev, last_event=None,
        is_held=True, is_watchlist=False, today_iso=_TODAY,
    )
    assert "今日" in out
    assert "⭐持仓" in out


# ---------------------------------------------------------------------------
# Calendar list
# ---------------------------------------------------------------------------

def test_earnings_calendar_filters_by_horizon_and_sorts():
    events = [
        EarningsEvent(ticker="GOOGL", release_date=_future(10), when="bmo"),
        EarningsEvent(ticker="AAPL",  release_date=_future(3),  when="amc"),
        EarningsEvent(ticker="NVDA",  release_date=_future(4),  when="amc"),
        # Past — must be excluded.
        EarningsEvent(ticker="META",  release_date=_future(-1), when="amc"),
        # Out of horizon — must be excluded.
        EarningsEvent(ticker="MSFT",  release_date=_future(30), when="amc"),
    ]
    out = format_earnings_calendar(
        events, horizon_days=14,
        held={"AAPL"}, watchlist={"GOOGL"},
        today_iso=_TODAY,
    )
    assert "未来 14 天财报日历 · 3 只标的" in out
    aapl_idx = out.find("AAPL")
    nvda_idx = out.find("NVDA")
    googl_idx = out.find("GOOGL")
    # Date order: AAPL (+3) < NVDA (+4) < GOOGL (+10)
    assert 0 < aapl_idx < nvda_idx < googl_idx, out
    assert "META" not in out and "MSFT" not in out


def test_earnings_calendar_marks_held_and_watchlist():
    events = [
        EarningsEvent(ticker="AAPL",  release_date=_future(2), when="amc"),
        EarningsEvent(ticker="NVDA",  release_date=_future(3), when="bmo"),
        EarningsEvent(ticker="GOOGL", release_date=_future(5), when="amc"),
    ]
    out = format_earnings_calendar(
        events, horizon_days=14,
        held={"AAPL", "NVDA"},          # both held
        watchlist={"NVDA", "GOOGL"},    # NVDA also watchlist, GOOGL only
        today_iso=_TODAY,
    )
    # AAPL: held only
    aapl_line = [ln for ln in out.split("\n") if "AAPL" in ln][0]
    assert "⭐持仓" in aapl_line
    assert "⭐watchlist" not in aapl_line   # held trumps watchlist
    # NVDA: held trumps watchlist
    nvda_line = [ln for ln in out.split("\n") if "NVDA" in ln][0]
    assert "⭐持仓" in nvda_line
    assert "⭐watchlist" not in nvda_line
    # GOOGL: watchlist only
    googl_line = [ln for ln in out.split("\n") if "GOOGL" in ln][0]
    assert "⭐持仓" not in googl_line
    assert "⭐watchlist" in googl_line


def test_earnings_calendar_empty_list_message():
    out = format_earnings_calendar(
        [], horizon_days=14, held=set(), watchlist=set(),
        today_iso=_TODAY,
    )
    assert "未来 14 天财报日历" in out
    assert "无 watchlist 或持仓 标的发财报" in out


def test_earnings_calendar_all_past_or_too_far():
    """Calendar with events but none in horizon → empty-list message."""
    events = [
        EarningsEvent(ticker="META", release_date=_future(-1), when="amc"),
        EarningsEvent(ticker="MSFT", release_date=_future(40), when="amc"),
    ]
    out = format_earnings_calendar(
        events, horizon_days=14, held={"META"}, watchlist={"MSFT"},
        today_iso=_TODAY,
    )
    assert "无 watchlist 或持仓 标的发财报" in out
    assert "META" not in out
    assert "MSFT" not in out
