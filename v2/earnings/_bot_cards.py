"""Inline bot-card formatters for /earnings and earnings_calendar.

Kept in v2/earnings/ (not v2/bot/responders.py) so they can be imported
and unit-tested without dragging in the production-only v2.data module.

Stage 5 will lift these into v2/reporting/formatters.py alongside the
other ``format_*`` functions; the import in responders.py is the single
seam to update at that point.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Iterable

from v2.earnings.models import EarningsEvent, EarningsHistorical


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WHEN_LABEL = {
    "bmo": "盘前",
    "amc": "盘后",
    "unknown": "时间未公布",
}

_SURPRISE_EMOJI = {
    "BEAT": "🟢",
    "MISS": "🔴",
    "MEET": "🟡",
    "UNKNOWN": "❔",
}


def _badge(is_held: bool, is_watchlist: bool) -> str:
    """⭐ chip(s) for the user's relationship to a ticker."""
    chips = []
    if is_held:
        chips.append("⭐持仓")
    if is_watchlist:
        chips.append("⭐watchlist")
    return " ".join(chips)


def _fmt_money(v: float | None) -> str:
    """$ in billions, 2 d.p. None → '—'."""
    if v is None:
        return "—"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def _fmt_eps(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:.2f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return ""
    return f" 🟢 +{v:.1%}" if v >= 0 else f" 🔴 {v:.1%}"


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_earnings_view(
    ticker: str,
    *,
    next_event: EarningsEvent | None,
    last_event: EarningsHistorical | None,
    is_held: bool,
    is_watchlist: bool,
    today_iso: str | None = None,
) -> str:
    """Single-ticker earnings card.

    Composes ``next`` (yfinance forward calendar) and ``last`` (FD historical)
    into one HTML card. Any missing block degrades gracefully — caller does
    not need to pre-filter for completeness.
    """
    ticker = ticker.upper()
    today_iso = today_iso or date.today().isoformat()

    lines: list[str] = [
        f"<b>📞 {html.escape(ticker)} 财报</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # --- Next release ---
    if next_event is not None:
        when = _WHEN_LABEL.get(next_event.when, next_event.when)
        d_minus = next_event.d_minus(today_iso)
        d_label = f"D-{d_minus}" if d_minus > 0 else "今日" if d_minus == 0 else f"D+{-d_minus}"
        lines.append(
            f"下次：<code>{next_event.release_date}</code> {when} (<b>{d_label}</b>)"
        )
        extras: list[str] = []
        if next_event.eps_estimate is not None:
            extras.append(f"预期 EPS <code>{_fmt_eps(next_event.eps_estimate)}</code>")
        if next_event.revenue_estimate is not None:
            extras.append(f"预期营收 <code>{_fmt_money(next_event.revenue_estimate)}</code>")
        if extras:
            lines.append("  " + " · ".join(extras))
    else:
        lines.append("<i>暂未发现即将的财报安排</i>")

    badge = _badge(is_held, is_watchlist)
    if badge:
        lines.append(badge)

    lines.append("")

    # --- Last filing ---
    if last_event is not None and last_event.has_quarterly_data:
        surprise_emoji = _SURPRISE_EMOJI.get(last_event.eps_surprise, "•")
        lines.append(
            f"<b>上次 ({last_event.report_period})</b> "
            f"{surprise_emoji} {last_event.eps_surprise}"
        )
        rev_pct = last_event.revenue_surprise_pct()
        eps_pct = last_event.eps_surprise_pct()
        if last_event.revenue_actual is not None and last_event.revenue_estimate is not None:
            lines.append(
                f"  💰 营收 <code>{_fmt_money(last_event.revenue_actual)}</code> "
                f"(预期 <code>{_fmt_money(last_event.revenue_estimate)}</code>)"
                f"{_fmt_pct(rev_pct)}"
            )
        if last_event.eps_actual is not None and last_event.eps_estimate is not None:
            lines.append(
                f"  📊 EPS <code>{_fmt_eps(last_event.eps_actual)}</code> "
                f"(预期 <code>{_fmt_eps(last_event.eps_estimate)}</code>)"
                f"{_fmt_pct(eps_pct)}"
            )
    else:
        lines.append("<i>暂无最近财报数据</i>")

    return "\n".join(lines)


def format_earnings_calendar(
    events: Iterable[EarningsEvent],
    *,
    horizon_days: int,
    held: set[str],
    watchlist: set[str],
    today_iso: str | None = None,
) -> str:
    """Multi-ticker forward calendar list, sorted by release date."""
    today_iso = today_iso or date.today().isoformat()

    rows: list[tuple[str, EarningsEvent]] = []
    for ev in events:
        d_minus = ev.d_minus(today_iso)
        if 0 <= d_minus <= horizon_days:
            rows.append((ev.release_date, ev))
    rows.sort(key=lambda r: r[0])

    if not rows:
        return (
            f"<b>📅 未来 {horizon_days} 天财报日历</b>\n"
            f"<i>无 watchlist 或持仓 标的发财报</i>"
        )

    lines: list[str] = [
        f"<b>📅 未来 {horizon_days} 天财报日历 · {len(rows)} 只标的</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for _, ev in rows:
        ticker = ev.ticker.upper()
        when = _WHEN_LABEL.get(ev.when, ev.when)
        is_held = ticker in held
        is_wl = ticker in watchlist and not is_held
        badge = _badge(is_held, is_wl)
        line = f"<code>{ev.release_date}</code>  <b>{html.escape(ticker)}</b> {when}"
        if badge:
            line = f"{line}  {badge}"
        lines.append(line)
    return "\n".join(lines)
