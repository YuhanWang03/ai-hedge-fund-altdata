"""SEC 8-K scanner — Mon-Fri 17:05 ET.

The eleventh scheduled agent. For each ticker in
(watchlist ∪ Alpaca holdings), pulls 8-K filings filed today, classifies
items by Stage-0 priority table, skips 2.02-only earnings filings (⑧
Earnings Summaries already handles those), runs LLM extraction on 5.02
items, and pushes one Telegram card per remaining filing.

Calibration recap (Stage 0 task 4 real-data):
- ~3 events/day across 10-ticker universe → daily cron is right cadence
- HPE-style multi-item filings get one card with priority = max(items)
- 5.02 LLM extractor escalates to P0 when senior exec confirmed

Card formatting is inline here for Stage 2 (Stage 5 will lift it into
``v2.reporting.format_sec_*`` mirroring the Phase 1/2 pattern).
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
from v2.sec.models import EightKEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


_TZ_ET = ZoneInfo("US/Eastern")


# Tier → event_kind mapping for the priority layer.
_KIND_BY_TIER = {
    "P0": "sec_8k_p0",
    "P1": "sec_8k_p1",
    "P2": "sec_8k_p2",
    "P3": "sec_8k_p3",
}


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def _resolve_universe() -> tuple[list[str], set[str], set[str]]:
    """Return (sorted_universe, held_tickers, watchlist_tickers)."""
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
# Inline card formatter (Stage 5 will lift to v2/reporting)
# ---------------------------------------------------------------------------

def _tier_emoji(tier: str) -> str:
    return {"P0": "🚨", "P1": "📋", "P2": "📎", "P3": "📌"}.get(tier, "📌")


def _format_8k_card(
    event: EightKEvent,
    *,
    is_held: bool,
    is_watchlist: bool,
) -> str:
    """Render one 8-K filing as a single card.

    Items listed in document order with per-item tier badge. The 2.02
    item, if present alongside other material items, gets a "(⑧ 处理)"
    annotation so the reader knows the earnings data is covered by
    the 21:00 ET earnings cron — not missed.
    """
    filing = event.filing
    tier_top = event.max_priority_tier
    emoji = _tier_emoji(tier_top)

    badge = "🟢 持仓股" if is_held else "👁 关注列表" if is_watchlist else ""

    lines: list[str] = [
        f"<b>{emoji} SEC 8-K · {filing.ticker} · {tier_top}</b>",
        f"申报日：<code>{filing.filing_date}</code>"
        + (" <i>(amendment)</i>" if filing.is_amendment else ""),
    ]
    if badge:
        lines.append(badge)

    lines.append("")
    lines.append("<b>项目</b>")
    for item in event.items:
        emoji_i = _tier_emoji(item.priority_tier)
        annotation = "  <i>(数据由 ⑧ 处理)</i>" if item.code == "2.02" else ""
        lines.append(
            f"  {emoji_i} <code>{item.code}</code> "
            f"[{item.priority_tier}] {item.description}{annotation}"
        )

    # 5.02 extraction summary if present
    item_5_02 = next(
        (it for it in event.items if it.code == "5.02"), None,
    )
    if item_5_02 and item_5_02.extracted_meta:
        meta = item_5_02.extracted_meta
        departures = meta.get("departures") or []
        appointments = meta.get("appointments") or []
        if departures or appointments:
            lines.append("")
            lines.append("<b>5.02 抽取</b>")
            for d in departures[:3]:
                name = d.get("name", "")
                title = d.get("title", "")
                if name:
                    lines.append(f"  📤 离职：{name} ({title})")
            for a in appointments[:3]:
                name = a.get("name", "")
                title = a.get("title", "")
                if name:
                    lines.append(f"  📥 任命：{name} ({title})")

    return "\n".join(lines)


def _emit_one(
    notifier: TelegramNotifier,
    trace,
    event: EightKEvent,
    *,
    is_held: bool,
    is_watchlist: bool,
) -> None:
    """Compute priority + render + push for one 8-K filing."""
    tier = event.max_priority_tier
    kind = _KIND_BY_TIER[tier]

    # 5.02 senior-exec flag for the P0 priority bump
    has_senior_exec = False
    for it in event.items:
        if it.code == "5.02":
            has_senior_exec = bool(it.extracted_meta.get("has_senior_exec"))
            break

    metadata = {
        "is_amendment": event.filing.is_amendment,
        "has_senior_exec": has_senior_exec,
        "is_held_position": is_held,
        "is_watchlist": is_watchlist,
    }
    priority = compute_importance(kind, metadata)

    text = _format_8k_card(event, is_held=is_held, is_watchlist=is_watchlist)
    notifier.send_text(
        text,
        trace=trace,
        title=f"SEC 8-K · {event.filing.ticker} · {tier}",
        tickers=[event.filing.ticker],
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("SEC 8-K")
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
        agent="sec", intent="sec_8k_view",
        text=f"(自动推送) SEC 8-K 扫描 · {len(universe)} 只 · {today_iso}",
        responder_name="_r_sec_8k",
    ) as trace:
        result = run_sec_scan(universe, today_iso)
        trace.emit(
            "chat_message", role="bot",
            text=f"SEC 8-K 扫描 · {len(universe)} 只 · "
                 f"{len(result.eight_k_events)} 个 8-K filing · "
                 f"{len(result.warnings)} 警告",
        )

        # Filter: skip 2.02-only earnings filings (handled by ⑧)
        material_events = [
            e for e in result.eight_k_events if not e.is_2_02_only
        ]
        skipped_earnings = len(result.eight_k_events) - len(material_events)

        if not material_events:
            logger.info(
                "SEC 8-K: %d ticker, %d total filings, %d 2.02-only skipped, "
                "0 material — silent exit",
                len(universe), len(result.eight_k_events), skipped_earnings,
            )
            return 0

        notifier = TelegramNotifier(archive=archive)
        for event in material_events:
            ticker = event.filing.ticker
            is_held = ticker in held
            is_wl = (ticker in watchlist) and not is_held
            try:
                _emit_one(notifier, trace, event, is_held=is_held, is_watchlist=is_wl)
            except Exception as exc:
                logger.warning(
                    "8-K push failed for %s acc=%s: %s",
                    ticker, event.filing.accession_number, exc,
                )

    logger.info(
        "SEC 8-K complete: %d universe / %d filings / %d pushed / "
        "%d 2.02-skipped / %d warnings",
        len(universe), len(result.eight_k_events), len(material_events),
        skipped_earnings, len(result.warnings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
