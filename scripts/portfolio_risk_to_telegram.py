"""Portfolio risk daily report — Mon-Fri 18:30 ET.

The ninth scheduled agent. Builds a :class:`RiskReport` covering
positions / concentration / sector exposure / P&L / drawdown / 7-day
earnings risk, and pushes one Telegram card. Priority is computed from
the report:

    base = 55 (P2)
    + daily_pnl_pct ≤ -5%    → +30  (→ P0)
    + daily_pnl_pct ≤ -2%    → +10  (→ P1)
    + top_1_pct ≥ 30%        → +20  (→ P1)
    + top_1_pct ≥ 20%        → +10
    + max_drawdown_pct ≥ 10% → +15
    + n_earnings_next_7d ≥ 3 → +10

Caveats (Stage-2 plan):
- Alpaca unavailable → cron still pushes a P2 "组合数据暂不可用" card
  using the warnings populated by ``build_risk_report``.
- The card format here is intentionally a Stage-2 inline ``_format_*``
  helper; Stage 5 will lift it into ``v2/reporting/formatters.py``
  alongside the earnings formatters.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from v2.archive import Archive
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


# ---------------------------------------------------------------------------
# Inline card formatter (Stage 5 will lift to v2/reporting/formatters.py)
# ---------------------------------------------------------------------------

_SECTOR_NAME = {
    "SMH":  "半导体",
    "XLK":  "科技",
    "XLF":  "金融",
    "XLV":  "医药",
    "XLP":  "消费 staples",
    "XLE":  "能源",
    "XLC":  "通信",
    "XLI":  "工业",
    "KWEB": "中概",
    "SPY":  "大盘",
    "OTHER":"其他",
}


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


def _hhi_label(hhi: float) -> str:
    if hhi >= 0.25:
        return "高度集中"
    if hhi >= 0.15:
        return "中等集中"
    if hhi >= 0.08:
        return "适度分散"
    return "高度分散"


def _format_risk_card(report: RiskReport) -> str:
    """Render a RiskReport into a Telegram HTML message.

    Sub-sections degrade independently — concentration / exposure can
    render with positions data even if drawdown is None.
    """
    lines: list[str] = [
        f"<b>💼 组合风险 · {report.snapshot_date}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    total = report.portfolio_value + report.cash
    if total > 0:
        lines.append(
            f"组合价值 <code>{_fmt_money(total)}</code> · "
            f"现金 <code>{_fmt_money(report.cash)}</code> "
            f"({report.cash_pct:.1%})"
        )
    else:
        lines.append("<i>账户暂无数据</i>")

    # ----- P&L -----
    pnl = report.pnl
    if pnl.daily_pnl_pct is not None and pnl.daily_pnl is not None:
        sign = "+" if pnl.daily_pnl >= 0 else "-"
        lines.append(
            f"今日 P/L {_fmt_signed_pct(pnl.daily_pnl_pct)} "
            f"({sign}<code>{_fmt_money(abs(pnl.daily_pnl))}</code>)"
        )
    if pnl.weekly_pnl_pct is not None or pnl.monthly_pnl_pct is not None:
        wk = (f"本周 {pnl.weekly_pnl_pct:+.2%}"
              if pnl.weekly_pnl_pct is not None else "本周 数据不足")
        mo = (f"本月 {pnl.monthly_pnl_pct:+.2%}"
              if pnl.monthly_pnl_pct is not None else "本月 数据不足")
        lines.append(f"{wk} · {mo}")

    # ----- Concentration -----
    if report.positions:
        c = report.concentration
        top_ticker = report.positions[0].ticker
        warn_top1 = " ⚠️" if c.top_1_pct >= 0.20 else ""
        lines.append("")
        lines.append("<b>📊 集中度</b>")
        lines.append(
            f"  Top 1: <b>{top_ticker}</b> {c.top_1_pct:.1%}{warn_top1}"
        )
        lines.append(f"  Top 5: {c.top_5_pct:.1%}")
        lines.append(f"  HHI: {c.hhi:.2f} ({_hhi_label(c.hhi)})")

        # ----- Exposure -----
        e = report.exposure
        if e.by_sector:
            lines.append("")
            lines.append("<b>🏭 行业暴露</b>")
            # Sort by weight descending; cap at 4 lines + "其他"
            sorted_buckets = sorted(
                e.by_sector.items(), key=lambda kv: kv[1], reverse=True,
            )
            for etf, w in sorted_buckets[:4]:
                name = _SECTOR_NAME.get(etf, etf)
                warn = " ⚠️" if w >= 0.30 else ""
                lines.append(f"  {etf} ({name}): {w:.1%}{warn}")
            if len(sorted_buckets) > 4:
                rest = sum(w for _, w in sorted_buckets[4:])
                lines.append(f"  其余: {rest:.1%}")

    # ----- Drawdown -----
    dd = report.drawdown
    if dd.max_drawdown_pct is not None:
        lines.append("")
        peak = (f" (峰值 {_fmt_money(dd.peak_value)} @ {dd.peak_date})"
                if dd.peak_value is not None and dd.peak_date is not None
                else "")
        lines.append(
            f"<b>📉 回撤 (1M)</b> 当前 {dd.current_drawdown_pct:+.2%} · "
            f"最大 {dd.max_drawdown_pct:+.2%}{peak}"
        )

    # ----- Earnings risk -----
    if report.earnings_risk_next_7d:
        lines.append("")
        lines.append(f"<b>📅 未来 7 天财报风险</b> ({len(report.earnings_risk_next_7d)} 只)")
        for item in report.earnings_risk_next_7d[:5]:
            tag = f"D-{item.days_until}" if item.days_until > 0 else "今日"
            lines.append(f"  <code>{item.release_date}</code> <b>{item.ticker}</b> ({tag})")

    # ----- Alerts -----
    alerts = _build_alerts(report)
    if alerts:
        lines.append("")
        lines.append("⚠️ <i>" + " / ".join(alerts) + "</i>")

    # ----- Warnings (data quality) -----
    if report.warnings:
        lines.append("")
        lines.append("<i>⚠ 数据不全：</i>")
        for w in report.warnings[:3]:
            lines.append(f"<i>  • {w[:80]}</i>")

    return "\n".join(lines)


def _build_alerts(report: RiskReport) -> list[str]:
    """Short alert strings appended at the bottom of the card."""
    out: list[str] = []
    if report.positions:
        c = report.concentration
        if c.top_1_pct >= 0.30:
            out.append(
                f"单票 {report.positions[0].ticker} > 30%"
            )
        elif c.top_1_pct >= 0.20:
            out.append(
                f"单票 {report.positions[0].ticker} > 20%"
            )
    if report.exposure.largest_sector_pct >= 0.30:
        out.append(
            f"{report.exposure.largest_sector} 行业 > 30%"
        )
    if (report.drawdown.max_drawdown_pct is not None
            and abs(report.drawdown.max_drawdown_pct) >= 0.10):
        out.append(f"1M 回撤 > 10%")
    if (report.pnl.daily_pnl_pct is not None
            and report.pnl.daily_pnl_pct <= -0.05):
        out.append(f"单日亏损 ≥ 5%")
    if len(report.earnings_risk_next_7d) >= 3:
        out.append(f"未来 7 天 {len(report.earnings_risk_next_7d)} 只财报")
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("Portfolio Risk")
def main() -> int:
    load_dotenv()
    install_all()

    archive = Archive("portfolio")
    today_iso = datetime.now(_TZ_ET).date().isoformat()

    with capture_trace_with_framing(
        agent="portfolio", intent="portfolio_risk_view",
        text=f"(自动推送) 组合风险 · {today_iso}",
        responder_name="_r_portfolio_risk",
    ) as trace:
        report = build_risk_report(today_iso=today_iso)
        trace.emit("chat_message", role="bot",
                   text=f"组合风险卡 · {len(report.positions)} 持仓 · "
                        f"{len(report.warnings)} 警告")

    priority = compute_importance(
        "portfolio_risk",
        {
            "daily_pnl_pct":     report.pnl.daily_pnl_pct or 0.0,
            "top_1_pct":         report.concentration.top_1_pct,
            "max_drawdown_pct":  report.drawdown.max_drawdown_pct or 0.0,
            "n_earnings_next_7d":len(report.earnings_risk_next_7d),
        },
    )
    text = _format_risk_card(report)

    notifier = TelegramNotifier(archive=archive)
    notifier.send_text(
        text,
        trace=trace,
        title=f"组合风险 · {today_iso}",
        tickers=[p.ticker for p in report.positions][:10],
        priority=priority,
    )

    logger.info(
        "pushed risk card %s (score=%d tier=%s positions=%d warnings=%d)",
        today_iso, priority.score, priority.tier,
        len(report.positions), len(report.warnings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
