"""Portfolio weekly review — Fri 19:00 ET.

The tenth scheduled agent. Renders a weekly recap focusing on
portfolio-level numbers (weekly return, drawdown trajectory, sector
exposure drift) plus a small matplotlib equity-curve chart showing
the 1-month peak and current position.

Per-position weekly attribution ("best stock / worst stock this week")
is intentionally skipped in v1: Alpaca doesn't expose per-position
equity history, and faking it from ``unrealized_pl_pct`` (since entry,
not since week start) would be misleading. Full attribution would need
a daily ``positions_snapshot`` table — deferred to Phase 2.5 if asked.

Always P1 — weekly recap is operator-visible regardless of whether any
single number trips a P0 alert. (For mid-week P0 risks, the daily
⑨ Portfolio Risk cron at 18:30 ET fires.)
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from v2.archive import Archive
from v2.broker import AlpacaUnavailable, get_portfolio_history
from v2.observability import capture_trace_with_framing, install_all
from v2.portfolio import RiskReport, build_risk_report
from v2.reporting import TelegramNotifier, notify_on_error
from v2.reporting.priority import compute_importance


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


_TZ_ET = ZoneInfo("US/Eastern")


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


def _fmt_signed_pct(v: float | None) -> str:
    if v is None:
        return "数据不足"
    if v > 0:
        return f"🟢 +{v:.2%}"
    if v < 0:
        return f"🔴 {v:.2%}"
    return f"🟡 {v:.2%}"


# ---------------------------------------------------------------------------
# Inline weekly card formatter
# ---------------------------------------------------------------------------

def _format_weekly_card(report: RiskReport) -> str:
    """Render the weekly recap message. Per-position attribution
    intentionally omitted — Alpaca lacks the data."""
    today = report.snapshot_date
    pnl = report.pnl
    dd = report.drawdown

    lines: list[str] = [
        f"<b>📊 周 P&amp;L 复盘 · {today}</b>",
        "<i>(截至昨日收盘的口径)</i>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # ----- Portfolio total -----
    # Same layout as ⑨ for consistency — TOTAL (invested + cash) header.
    if report.portfolio_value > 0:
        lines.append(
            f"组合价值 <code>{_fmt_money(report.portfolio_value)}</code> "
            f"(持仓 <code>{_fmt_money(report.invested_value)}</code> · "
            f"现金 <code>{_fmt_money(report.cash)}</code>, "
            f"{report.cash_pct:.1%})"
        )

    # ----- Weekly + monthly returns -----
    lines.append("")
    lines.append(f"<b>本周回报</b> {_fmt_signed_pct(pnl.weekly_pnl_pct)}")
    lines.append(f"<b>本月回报</b> {_fmt_signed_pct(pnl.monthly_pnl_pct)}")

    # ----- Drawdown -----
    if dd.max_drawdown_pct is not None:
        lines.append("")
        lines.append(
            f"<b>📉 1M 最大回撤</b> {dd.max_drawdown_pct:+.2%}"
        )
        if dd.peak_value is not None and dd.peak_date is not None:
            lines.append(
                f"  峰值 <code>{_fmt_money(dd.peak_value)}</code> @ "
                f"<code>{dd.peak_date}</code>"
            )
        if dd.current_drawdown_pct is not None:
            lines.append(
                f"  当前距峰 {dd.current_drawdown_pct:+.2%}"
            )

    # ----- Sector exposure (Top 3) -----
    if report.exposure.by_sector:
        lines.append("")
        lines.append("<b>🏭 主要行业暴露</b>")
        sorted_buckets = sorted(
            report.exposure.by_sector.items(),
            key=lambda kv: kv[1], reverse=True,
        )
        for etf, w in sorted_buckets[:3]:
            lines.append(f"  {etf}: {w:.1%}")

    # ----- Earnings ahead -----
    if report.earnings_risk_next_7d:
        lines.append("")
        lines.append(
            f"<b>📅 下周财报：</b>{len(report.earnings_risk_next_7d)} 只"
        )
        for item in report.earnings_risk_next_7d[:5]:
            lines.append(
                f"  <code>{item.release_date}</code> <b>{item.ticker}</b>"
            )

    # ----- Caveat about per-position attribution -----
    lines.append("")
    lines.append(
        "<i>（per-position 周表现归因待开发——Alpaca 不提供每个持仓的历史曲线，"
        "需自建每日快照表 → Phase 2.5）</i>"
    )

    # ----- Data warnings -----
    if report.warnings:
        lines.append("")
        lines.append("<i>⚠ 数据不全：</i>")
        for w in report.warnings[:3]:
            lines.append(f"<i>  • {w[:80]}</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Equity curve chart
# ---------------------------------------------------------------------------

def _render_equity_chart(title: str) -> bytes | None:
    """Render a 1-month equity curve PNG. Returns None if history is
    unavailable — caller falls back to text-only push."""
    try:
        history = get_portfolio_history(period="1M", timeframe="1D")
    except AlpacaUnavailable as exc:
        logger.info("equity chart skipped (Alpaca unavailable: %s)", exc)
        return None
    except Exception as exc:
        logger.warning("equity chart fetch failed: %s", exc)
        return None

    equity = history.get("equity") or []
    if len(equity) < 2:
        return None

    # Local import — matplotlib is already loaded by v2.reporting.formatters
    # in production; keeping it function-local lets the cron's --test
    # mode still inspect main() without the matplotlib backend bootstrapping.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    timestamps = history.get("timestamp") or list(range(len(equity)))
    dates = [datetime.fromtimestamp(int(t)) for t in timestamps[:len(equity)]]

    peak_idx = max(range(len(equity)), key=lambda i: equity[i])

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(dates, equity, color="#1f77b4", linewidth=2)
    ax.fill_between(dates, equity, min(equity), alpha=0.15, color="#1f77b4")

    # Mark the peak
    ax.scatter([dates[peak_idx]], [equity[peak_idx]],
               color="#d62728", s=60, zorder=5)
    ax.annotate(
        f"峰值 ${equity[peak_idx]:,.0f}",
        xy=(dates[peak_idx], equity[peak_idx]),
        xytext=(8, 8), textcoords="offset points",
        fontsize=9, color="#d62728",
    )

    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel("Equity (USD)")
    ax.grid(True, alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("Portfolio Weekly")
def main() -> int:
    load_dotenv()
    install_all()

    archive = Archive("portfolio")
    today_iso = datetime.now(_TZ_ET).date().isoformat()

    with capture_trace_with_framing(
        agent="portfolio", intent="portfolio_risk_view",
        text=f"(自动推送) 组合周报 · {today_iso}",
        responder_name="_r_portfolio_weekly",
    ) as trace:
        report = build_risk_report(today_iso=today_iso)
        trace.emit("chat_message", role="bot",
                   text=f"组合周报 · {len(report.positions)} 持仓")

    # Weekly recap is always P1 — operator visibility.
    priority = compute_importance("portfolio_risk", {})
    # Force P1 floor: weekly recap shouldn't get demoted to P2 even on
    # a clean week. Re-tag explicitly.
    if priority.tier == "P2":
        # Synthesize P1 with a "weekly_recap" reason so the trace shows
        # why the floor was applied.
        from v2.reporting.priority import PriorityResult
        priority = PriorityResult(
            score=65, tier="P1",
            reasons=list(priority.reasons) + ["+10_weekly_recap_floor"],
        )

    text = _format_weekly_card(report)
    chart = _render_equity_chart(f"组合权益曲线 · 1M · {today_iso}")

    notifier = TelegramNotifier(archive=archive)
    if chart is not None:
        notifier.send_photo(
            chart, caption=text,
            trace=trace,
            title=f"组合周报 · {today_iso}",
            tickers=[p.ticker for p in report.positions][:10],
            priority=priority,
        )
    else:
        notifier.send_text(
            text,
            trace=trace,
            title=f"组合周报 · {today_iso}",
            tickers=[p.ticker for p in report.positions][:10],
            priority=priority,
        )

    logger.info(
        "pushed weekly recap %s (score=%d tier=%s chart=%s)",
        today_iso, priority.score, priority.tier,
        "yes" if chart else "no",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
