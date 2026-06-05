"""SEC Form 4 (insider transactions) scanner — Mon-Fri 17:45 ET.

The twelfth scheduled agent. For each ticker in (watchlist ∪ holdings),
pulls Form 4 filings filed today, classifies transactions:

- P/S signal codes → individual Telegram cards (priority by magnitude /
  insider role / 10b5-1 plan status per Stage 0 priority spec)
- A/M/F/G/C noise codes → batched into ``form4_noise_summary`` and
  archive-only. The Phase 3.5 weekly insider digest will consume them.
- Same-day same-direction P/S clusters (≥3 distinct insiders) → one
  cluster card with elevated priority.

Cron time note: Stage 2 prompt proposed 17:30 ET but that collides
with ① Daily Screen. Slotted at 17:45 ET (clean — between ② Anomaly
Monitor 17:35 and ③ Lateral / ④ Institutional 18:00).

Card format inline; Stage 5 will lift to ``v2.reporting.format_sec_*``.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from v2.archive import Archive
from v2.bot import state as bot_state
from v2.observability import capture_trace_with_framing, install_all
from v2.reporting import TelegramNotifier, notify_on_error
from v2.reporting.priority import compute_importance
from v2.sec import run_sec_scan
from v2.sec.models import Form4Cluster, Form4Transaction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


_TZ_ET = ZoneInfo("US/Eastern")


def _resolve_universe() -> tuple[list[str], set[str], set[str]]:
    """Same shape as the 8-K cron — held + watchlist union."""
    watchlist = {row["ticker"].upper() for row in bot_state.watchlist_list()}

    held: set[str] = set()
    try:
        from v2.broker import AlpacaUnavailable, get_portfolio
        portfolio = get_portfolio()
        held = {p["symbol"].upper() for p in portfolio.get("positions", [])}
    except Exception as exc:
        logger.info("Alpaca unavailable, scanning watchlist-only: %s", exc)

    return sorted(watchlist | held), held, watchlist


# ---------------------------------------------------------------------------
# Inline card formatters (Stage 5 lift target)
# ---------------------------------------------------------------------------

def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "未披露"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _role_label(role: str | None) -> str:
    if not role:
        return ""
    pretty = {
        "CEO": "🔴 CEO", "CFO": "🔴 CFO", "COO": "🟠 COO",
        "Chairman": "🟠 Chairman", "President": "🟠 President",
        "GC": "🟡 General Counsel", "Director": "🟢 董事",
        "Officer": "🔵 高管", "10% holder": "🔵 10% 大股东",
    }
    return pretty.get(role, role)


def _format_form4_signal_card(
    tx: Form4Transaction,
    *,
    is_held: bool,
    is_watchlist: bool,
) -> str:
    """Single P or S transaction → one card."""
    f = tx.filing
    badge = "🟢 持仓股" if is_held else "👁 关注列表" if is_watchlist else ""

    if tx.transaction_code == "P":
        direction_label = "📥 内部人买入"
    else:
        direction_label = "📤 内部人卖出"

    plan_tag = "<i>(10b5-1 plan)</i>" if tx.is_10b5_1 else "<i>(discretionary)</i>"

    lines: list[str] = [
        f"<b>{direction_label} · {f.ticker}</b>",
        f"申报：<code>{f.filing_date}</code> · 交易：<code>{tx.transaction_date}</code>",
    ]
    if badge:
        lines.append(badge)
    lines.append("")

    role = _role_label(tx.insider_role)
    role_suffix = f" · {role}" if role else ""
    lines.append(f"申报人：<b>{tx.insider_name or '?'}</b>{role_suffix}")

    lines.append(
        f"交易：<code>{tx.shares:,.0f}</code> 股 × "
        f"{_fmt_usd(tx.price)}/股 = <b>{_fmt_usd(tx.transaction_usd)}</b> "
        f"{plan_tag}"
    )

    return "\n".join(lines)


def _format_cluster_card(
    cluster: Form4Cluster,
    *,
    is_held: bool,
    is_watchlist: bool,
) -> str:
    """Cluster card — N insiders same day same direction."""
    badge = "🟢 持仓股" if is_held else "👁 关注列表" if is_watchlist else ""

    if cluster.direction == "purchase":
        emoji, label = "📥", "内部人集群买入"
    else:
        emoji, label = "📤", "内部人集群卖出"

    lines: list[str] = [
        f"<b>{emoji} {label} · {cluster.ticker}</b>",
        f"日期：<code>{cluster.cluster_date}</code> · "
        f"{cluster.transaction_count} 笔 / {len(cluster.insider_names)} 人",
    ]
    if badge:
        lines.append(badge)
    lines.append("")

    lines.append(f"总金额：<b>{_fmt_usd(cluster.total_usd)}</b>")
    lines.append("")

    lines.append("<b>申报人</b>")
    for name in cluster.insider_names[:6]:
        lines.append(f"  • {name}")
    if len(cluster.insider_names) > 6:
        lines.append(f"  ... 另 {len(cluster.insider_names) - 6} 人")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------

def _push_signal(
    notifier: TelegramNotifier,
    trace,
    tx: Form4Transaction,
    *,
    is_held: bool,
    is_watchlist: bool,
) -> None:
    kind = "sec_form4_purchase" if tx.transaction_code == "P" else "sec_form4_sale"
    metadata = {
        "transaction_usd": tx.transaction_usd or 0.0,
        "insider_role": tx.insider_role,
        "is_10b5_1": tx.is_10b5_1,
        "is_held_position": is_held,
        "is_watchlist": is_watchlist,
    }
    priority = compute_importance(kind, metadata)
    text = _format_form4_signal_card(tx, is_held=is_held, is_watchlist=is_watchlist)

    direction = "买入" if tx.transaction_code == "P" else "卖出"
    notifier.send_text(
        text,
        trace=trace,
        title=f"Form 4 · {tx.filing.ticker} · {direction}",
        tickers=[tx.filing.ticker],
        priority=priority,
    )


def _push_cluster(
    notifier: TelegramNotifier,
    trace,
    cluster: Form4Cluster,
    *,
    is_held: bool,
    is_watchlist: bool,
) -> None:
    metadata = {
        "transaction_count": cluster.transaction_count,
        "direction": cluster.direction,
        "is_held_position": is_held,
        "is_watchlist": is_watchlist,
    }
    priority = compute_importance("sec_form4_cluster", metadata)
    text = _format_cluster_card(cluster, is_held=is_held, is_watchlist=is_watchlist)

    notifier.send_text(
        text,
        trace=trace,
        title=f"Form 4 集群 · {cluster.ticker} · {cluster.direction}",
        tickers=[cluster.ticker],
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("SEC Form 4")
def main() -> int:
    load_dotenv()
    install_all()

    universe, held, watchlist = _resolve_universe()
    if not universe:
        logger.info("Empty universe — nothing to scan.")
        return 0

    today_iso = datetime.now(_TZ_ET).date().isoformat()
    archive = Archive("sec")

    with capture_trace_with_framing(
        agent="sec", intent="sec_form4_view",
        text=f"(自动推送) SEC Form 4 扫描 · {len(universe)} 只 · {today_iso}",
        responder_name="_r_sec_form4",
    ) as trace:
        result = run_sec_scan(universe, today_iso)
        trace.emit(
            "chat_message", role="bot",
            text=f"SEC Form 4 扫描 · {len(universe)} 只 · "
                 f"{len(result.form4_signal_transactions)} signal · "
                 f"{len(result.form4_clusters)} cluster · "
                 f"{sum(len(c) for c in result.form4_noise_summary.values())} noise codes",
        )

        clusters = result.form4_clusters
        signals = result.form4_signal_transactions

        # Build cluster's (ticker, date) set so we skip individual cards
        # for transactions already represented in a cluster card.
        cluster_keys = {
            (c.ticker, c.cluster_date, c.direction) for c in clusters
        }

        # Filter individual transactions whose (ticker, date, direction)
        # is already a cluster — avoid double-notifying.
        individual_signals = [
            tx for tx in signals
            if (tx.filing.ticker, tx.filing.filing_date, tx.direction)
            not in cluster_keys
        ]

        if not clusters and not individual_signals:
            logger.info(
                "SEC Form 4: %d ticker, 0 signal cards (noise=%s) — silent exit",
                len(universe),
                {t: sum(c.values()) for t, c in result.form4_noise_summary.items()},
            )
            return 0

        notifier = TelegramNotifier(archive=archive)

        # Cluster cards first (more important)
        for cluster in clusters:
            ticker = cluster.ticker
            is_held = ticker in held
            is_wl = (ticker in watchlist) and not is_held
            try:
                _push_cluster(notifier, trace, cluster, is_held=is_held, is_watchlist=is_wl)
            except Exception as exc:
                logger.warning(
                    "cluster push failed for %s %s: %s",
                    ticker, cluster.cluster_date, exc,
                )

        # Individual signal cards
        for tx in individual_signals:
            ticker = tx.filing.ticker
            is_held = ticker in held
            is_wl = (ticker in watchlist) and not is_held
            try:
                _push_signal(notifier, trace, tx, is_held=is_held, is_watchlist=is_wl)
            except Exception as exc:
                logger.warning(
                    "Form 4 push failed for %s acc=%s: %s",
                    ticker, tx.filing.accession_number, exc,
                )

    logger.info(
        "SEC Form 4 complete: %d universe / %d signals / %d clusters / "
        "%d individual cards / %d warnings",
        len(universe), len(signals), len(clusters),
        len(individual_signals), len(result.warnings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
