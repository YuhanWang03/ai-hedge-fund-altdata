"""Unit tests for the earnings card formatters.

Source of truth since Stage 5 is ``v2.earnings._bot_cards``; the public
namespace ``v2.reporting.format_earnings_*`` re-exports from it. We
import from the implementation module so the tests stay sandbox-runnable
(``v2.reporting``'s package init pulls matplotlib + v2.lateral, which
transitively requires the production-only v2.data).

The wrapper responder (``v2.bot.responders.earnings_view`` /
``earnings_calendar``) is exercised manually by the operator on the
deploy box; the unit-test coverage here pins the formatting + filtering
semantics.
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
    format_earnings_pending,
    format_earnings_reminder,
    format_earnings_summary,
    format_earnings_view,
)
from v2.earnings.models import (   # noqa: E402
    EarningsEvent,
    EarningsHistorical,
    EarningsSummary,
)


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


# ---------------------------------------------------------------------------
# Stage 5 — cron-card formatters (reminder / summary / pending)
# ---------------------------------------------------------------------------
# Each "byte_equal" test renders the card via the public format_* helper AND
# inlines the pre-Stage-5 logic verbatim (from the scripts/earnings_*.py
# helpers as of commit 13f851e), then asserts both produce the same string.
# These pin format-equivalence so future stages can't silently regress the
# Telegram lockscreen layout.


# Pre-Stage-5 reference inlines, copied verbatim from the scripts.
_REF_TAG_EMOJI = {"D-3": "📅", "D-1": "⏰", "D-0": "🎯"}
_REF_WHEN_LABEL = {"bmo": "盘前", "amc": "盘后", "unknown": "时间未公布"}
_REF_SURPRISE_EMOJI = {"BEAT": "🟢", "MISS": "🔴", "MEET": "🟡", "UNKNOWN": "❔"}


def _ref_badge(is_held: bool, is_watchlist: bool) -> str:
    if is_held:
        return "🟢 持仓股"
    if is_watchlist:
        return "👁 关注列表"
    return ""


def _ref_format_reminder(event, *, tag, is_held, is_watchlist):
    emoji = _REF_TAG_EMOJI[tag]
    when = _REF_WHEN_LABEL.get(event.when, event.when)
    badge = _ref_badge(is_held, is_watchlist)
    lines = [
        f"<b>{emoji} 财报提醒 · {event.ticker} · {tag}</b>",
        f"发布日：<code>{event.release_date}</code>（{when}）",
    ]
    if badge:
        lines.append(badge)
    extras = []
    if event.eps_estimate is not None:
        extras.append(f"EPS 预期：<code>{event.eps_estimate:.2f}</code>")
    if event.revenue_estimate is not None:
        extras.append(f"营收预期：<code>${event.revenue_estimate / 1e9:.2f}B</code>")
    if extras:
        lines.append("")
        lines.extend(extras)
    return "\n".join(lines)


def _ref_eps_pct(s):
    if s.eps_actual is None or s.eps_estimate is None or s.eps_estimate == 0:
        return None
    return (s.eps_actual - s.eps_estimate) / abs(s.eps_estimate)


def _ref_rev_pct(s):
    if s.revenue_actual is None or s.revenue_estimate is None or s.revenue_estimate <= 0:
        return None
    return (s.revenue_actual - s.revenue_estimate) / s.revenue_estimate


def _ref_format_summary(s, *, is_held, is_watchlist):
    badge = _ref_badge(is_held, is_watchlist)
    emoji = _REF_SURPRISE_EMOJI.get(s.eps_surprise, "•")
    lines = [
        f"<b>{emoji} 财报发布 · {s.ticker} · {s.eps_surprise}</b>",
        f"报告期：<code>{s.report_period}</code> · 申报：<code>{s.filing_date}</code>",
    ]
    if badge:
        lines.append(badge)
    lines.append("")
    eps_pct = _ref_eps_pct(s)
    if s.eps_actual is not None and s.eps_estimate is not None:
        delta = f" ({eps_pct:+.1%})" if eps_pct is not None else ""
        lines.append(
            f"EPS：<code>{s.eps_actual:.2f}</code> vs 预期 "
            f"<code>{s.eps_estimate:.2f}</code>{delta}"
        )
    rev_pct = _ref_rev_pct(s)
    if s.revenue_actual is not None and s.revenue_estimate is not None:
        delta = f" ({rev_pct:+.1%})" if rev_pct is not None else ""
        lines.append(
            f"营收：<code>${s.revenue_actual / 1e9:.2f}B</code> vs 预期 "
            f"<code>${s.revenue_estimate / 1e9:.2f}B</code>{delta}"
        )
    if s.last_4q_surprises:
        streak = " → ".join(s.last_4q_surprises)
        lines.append(f"最近 4 季：<code>{streak}</code>")
    if s.bull or s.bear:
        lines.append("")
        if s.bull:
            lines.append(f"👍 {s.bull}")
        if s.bear:
            lines.append(f"👎 {s.bear}")
    if s.narrative:
        lines.append("")
        lines.append(f"<i>{s.narrative}</i>")
    if s.transcript_url:
        lines.append("")
        lines.append(f'📜 <a href="{s.transcript_url}">电话会记录</a>')
    return "\n".join(lines)


def _ref_format_pending(ticker, today_iso, is_held, is_watchlist):
    badge = _ref_badge(is_held, is_watchlist)
    lines = [
        f"<b>⏳ 财报数据待落地 · {ticker}</b>",
        f"发布日：<code>{today_iso}</code>",
    ]
    if badge:
        lines.append(badge)
    lines.append("")
    lines.append("<i>FD 实际数据尚未入库，明天 21:00 ET 自动重试。</i>")
    return "\n".join(lines)


# ---- format_earnings_reminder --------------------------------------------

def test_reminder_d3_held_byte_equal():
    ev = EarningsEvent(
        ticker="AAPL", release_date=_future(3), when="amc",
        eps_estimate=1.51, revenue_estimate=94.0e9,
    )
    new = format_earnings_reminder(ev, tag="D-3", is_held=True, is_watchlist=False)
    old = _ref_format_reminder(ev, tag="D-3", is_held=True, is_watchlist=False)
    assert new == old, f"\n--- new ---\n{new}\n--- old ---\n{old}"
    # Shape sanity
    assert "📅 财报提醒 · AAPL · D-3" in new
    assert "🟢 持仓股" in new
    assert "EPS 预期：<code>1.51</code>" in new
    assert "营收预期：<code>$94.00B</code>" in new


def test_reminder_d1_watchlist_byte_equal():
    ev = EarningsEvent(
        ticker="NVDA", release_date=_future(1), when="bmo",
        eps_estimate=0.75,
    )
    new = format_earnings_reminder(ev, tag="D-1", is_held=False, is_watchlist=True)
    old = _ref_format_reminder(ev, tag="D-1", is_held=False, is_watchlist=True)
    assert new == old
    assert "⏰ 财报提醒 · NVDA · D-1" in new
    assert "👁 关注列表" in new
    # No revenue line — revenue_estimate is None
    assert "营收预期" not in new


def test_reminder_d0_no_estimates_no_badge_byte_equal():
    ev = EarningsEvent(
        ticker="TSLA", release_date=_TODAY, when="unknown",
    )
    new = format_earnings_reminder(ev, tag="D-0", is_held=False, is_watchlist=False)
    old = _ref_format_reminder(ev, tag="D-0", is_held=False, is_watchlist=False)
    assert new == old
    assert "🎯 财报提醒 · TSLA · D-0" in new
    assert "时间未公布" in new
    # No empty estimates block at the end
    assert not new.endswith("\n")


# ---- format_earnings_summary ---------------------------------------------

def test_summary_beat_held_byte_equal():
    s = EarningsSummary(
        ticker="AAPL", report_period="2026-06-30", filing_date="2026-08-01",
        eps_surprise="BEAT",
        eps_actual=2.10, eps_estimate=1.95,
        revenue_actual=9.5e10, revenue_estimate=9.1e10,
        last_4q_surprises=["BEAT", "BEAT", "MISS", "BEAT"],
        bull="本季 BEAT 连续，Services 加速",
        bear="iPhone 出货指引偏保守",
        narrative="基本面持续超预期",
        transcript_url="https://example.com/q3-call",
    )
    new = format_earnings_summary(s, is_held=True, is_watchlist=False)
    old = _ref_format_summary(s, is_held=True, is_watchlist=False)
    assert new == old
    assert "🟢 财报发布 · AAPL · BEAT" in new
    assert "🟢 持仓股" in new
    assert "EPS：<code>2.10</code> vs 预期 <code>1.95</code> (+7.7%)" in new
    assert "营收：<code>$95.00B</code> vs 预期 <code>$91.00B</code> (+4.4%)" in new
    assert "BEAT → BEAT → MISS → BEAT" in new
    assert "👍 本季 BEAT 连续，Services 加速" in new
    assert "👎 iPhone 出货指引偏保守" in new
    assert "电话会记录" in new


def test_summary_miss_minimal_no_bull_bear_byte_equal():
    """MISS without bull/bear/narrative/transcript → numbers-only card."""
    s = EarningsSummary(
        ticker="META", report_period="2026-Q1", filing_date="2026-04-23",
        eps_surprise="MISS",
        eps_actual=4.10, eps_estimate=4.50,
        revenue_actual=3.8e10, revenue_estimate=3.95e10,
    )
    new = format_earnings_summary(s, is_held=False, is_watchlist=True)
    old = _ref_format_summary(s, is_held=False, is_watchlist=True)
    assert new == old
    assert "🔴 财报发布 · META · MISS" in new
    assert "👁 关注列表" in new
    # No bull/bear/narrative lines
    assert "👍" not in new
    assert "👎" not in new
    assert "电话会记录" not in new


def test_summary_no_actuals_byte_equal():
    """Summary with eps_surprise=UNKNOWN and no numeric fields — extremely
    degraded but still renders without crashing."""
    s = EarningsSummary(
        ticker="X", report_period="2026-Q2", filing_date="2026-07-30",
        eps_surprise="UNKNOWN",
    )
    new = format_earnings_summary(s, is_held=False, is_watchlist=False)
    old = _ref_format_summary(s, is_held=False, is_watchlist=False)
    assert new == old
    assert "❔ 财报发布 · X · UNKNOWN" in new
    # No EPS/revenue/streak lines
    assert "EPS" not in new
    assert "营收" not in new
    assert "最近 4 季" not in new


# ---- format_earnings_pending ---------------------------------------------

def test_pending_held_byte_equal():
    new = format_earnings_pending(
        "AAPL", today_iso=_TODAY, is_held=True, is_watchlist=False,
    )
    old = _ref_format_pending("AAPL", _TODAY, True, False)
    assert new == old
    assert "⏳ 财报数据待落地 · AAPL" in new
    assert "🟢 持仓股" in new
    assert "明天 21:00 ET 自动重试" in new


def test_pending_no_badge_byte_equal():
    new = format_earnings_pending(
        "TSLA", today_iso=_TODAY, is_held=False, is_watchlist=False,
    )
    old = _ref_format_pending("TSLA", _TODAY, False, False)
    assert new == old


# ---- All 5 formatters exposed under both module paths --------------------

def test_bot_cards_module_exposes_all_five_formatters():
    """v2/bot/responders.py imports from v2.earnings._bot_cards. Verify the
    Stage-5 module surface is the full set of 5 formatters."""
    from v2.earnings import _bot_cards
    for name in (
        "format_earnings_view",
        "format_earnings_calendar",
        "format_earnings_reminder",
        "format_earnings_summary",
        "format_earnings_pending",
    ):
        assert hasattr(_bot_cards, name), f"_bot_cards missing {name}"
        assert name in _bot_cards.__all__, f"{name} not in __all__"
