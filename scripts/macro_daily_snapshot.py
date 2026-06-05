"""Macro daily snapshot — Mon-Fri 16:30 ET.

The fourteenth scheduled agent. Pulls post-close ambient market levels
(VIX / DXY / WTI / Gold) from yfinance and Treasury rates / Fed Funds
(canonical EOD) from FRED, then routes the push through a priority
kind that reflects the dominant anomaly flag:

- ``macro_vix_spike``   (P0 base, +up to 20 by magnitude)  — VIX +20%
- ``macro_curve_flip``  (P1 base, +10 if inverted)         — T10Y2Y sign flip
- ``macro_snapshot_p3`` (P3)                                — default daily

VIX-elevated (+10% < pct < +20%) and rates_shocked (|ΔDGS10| ≥ 20bps)
also escalate to ``macro_curve_flip`` since both are P1-class
volatility signals — the kind name is the routing label, not a literal
event-type assertion.

Card formatter is inline here for Stage 2 (Stage 5 lifts to
``v2.reporting.format_macro_*``).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from v2.archive import Archive
from v2.macro import build_macro_snapshot
from v2.observability import capture_trace_with_framing, install_all
from v2.reporting import TelegramNotifier, notify_on_error
from v2.reporting.priority import compute_importance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


_TZ_ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Priority routing
# ---------------------------------------------------------------------------

def _classify(snap) -> tuple[str, dict]:
    """Return (priority_kind, metadata) for a snapshot.

    Highest-priority anomaly wins: VIX spike > curve flip > VIX
    elevated / rates shocked > default ambient.
    """
    if snap.vix_spike:
        return "macro_vix_spike", {
            "vix_pct_change_1d": snap.vix_pct_change_1d,
            "vix": snap.vix,
        }
    if snap.curve_flip:
        return "macro_curve_flip", {
            "t10y2y": snap.t10y2y,
            "t10y2y_prior": snap.t10y2y_prior,
        }
    if snap.vix_elevated or snap.rates_shocked:
        return "macro_curve_flip", {
            "vix_pct_change_1d": snap.vix_pct_change_1d,
            "rates_shocked": snap.rates_shocked,
        }
    return "macro_snapshot_p3", {}


# ---------------------------------------------------------------------------
# Inline card formatter (Stage 5 lift target)
# ---------------------------------------------------------------------------

def _fmt_pct(p: float | None, *, signed: bool = True) -> str:
    if p is None:
        return "—"
    sign = "+" if signed and p >= 0 else ""
    return f"{sign}{p:.2%}"


def _fmt_level(v: float | None, *, places: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{places}f}"


def _format_snapshot_card(snap, *, tier: str, kind: str) -> str:
    """Render the daily snapshot as a single Telegram-ready HTML body."""
    icon = {
        "macro_vix_spike":   "🚨 宏观警报",
        "macro_curve_flip":  "📉 宏观警报",
        "macro_snapshot_p3": "📊 宏观日终",
    }.get(kind, "📊 宏观日终")

    lines: list[str] = [
        f"<b>{icon} · {snap.snapshot_date}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # Markets
    lines.append("<b>市场</b>")
    vix_str = _fmt_level(snap.vix)
    vix_d = _fmt_pct(snap.vix_pct_change_1d)
    spike_tag = " <b>🚨 +20%</b>" if snap.vix_spike else (
        " ⚠️ 偏高" if snap.vix_elevated else ""
    )
    lines.append(f"  VIX: <code>{vix_str}</code> ({vix_d}){spike_tag}")
    lines.append(
        f"  DXY: <code>{_fmt_level(snap.dxy)}</code> · "
        f"WTI: <code>{_fmt_level(snap.wti_crude)}</code> · "
        f"Gold: <code>{_fmt_level(snap.gold, places=1)}</code>"
    )

    # Rates
    lines.append("<b>利率 (FRED EOD)</b>")
    ff_band = "—"
    if snap.fed_funds_upper is not None and snap.fed_funds_lower is not None:
        ff_band = f"{snap.fed_funds_lower:.2f}% – {snap.fed_funds_upper:.2f}%"
    lines.append(f"  Fed Funds: <code>{ff_band}</code>")
    lines.append(
        f"  2Y: <code>{_fmt_level(snap.dgs2)}%</code> · "
        f"10Y: <code>{_fmt_level(snap.dgs10)}%</code>"
    )

    curve_tag = ""
    if snap.curve_flip:
        curve_tag = " <b>📉 今日翻转</b>"
    elif snap.t10y2y is not None and snap.t10y2y < 0:
        curve_tag = " <i>(倒挂)</i>"
    lines.append(
        f"  10Y-2Y: <code>{_fmt_level(snap.t10y2y)}%</code>{curve_tag}"
    )

    if snap.rates_shocked:
        lines.append("  <b>⚠️ 10Y 单日 ≥ 20bps 异动</b>")

    # Warnings (data-source failures)
    if snap.warnings:
        lines.append("")
        lines.append("<i>⚠️ 数据不全:</i>")
        for w in snap.warnings[:6]:
            lines.append(f"  • <i>{w}</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Push helper
# ---------------------------------------------------------------------------

def _emit(notifier, trace, snap) -> None:
    kind, md = _classify(snap)
    priority = compute_importance(kind, md)

    text = _format_snapshot_card(snap, tier=priority.tier, kind=kind)
    notifier.send_text(
        text,
        trace=trace,
        title=f"宏观日终 · {snap.snapshot_date} · {priority.tier}",
        tickers=[],
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("Macro Daily Snapshot")
def main() -> int:
    load_dotenv()
    install_all()

    today_iso = datetime.now(_TZ_ET).date().isoformat()
    archive = Archive("macro")

    with capture_trace_with_framing(
        agent="macro", intent="macro_snapshot_view",
        text=f"(自动推送) 宏观日终快照 · {today_iso}",
        responder_name="_r_macro_snapshot",
    ) as trace:
        snap = build_macro_snapshot(today_iso)
        trace.emit(
            "chat_message", role="bot",
            text=(
                f"宏观日终 · VIX={snap.vix} ({snap.vix_pct_change_1d}) · "
                f"DGS10={snap.dgs10} · T10Y2Y={snap.t10y2y} · "
                f"warnings={len(snap.warnings)}"
            ),
        )

        notifier = TelegramNotifier(archive=archive)
        try:
            _emit(notifier, trace, snap)
        except Exception as exc:
            logger.warning("macro snapshot push failed: %s", exc)
            return 1

    logger.info(
        "Macro snapshot complete: VIX=%s ΔVIX=%s spike=%s flip=%s "
        "rates_shocked=%s warnings=%d",
        snap.vix, snap.vix_pct_change_1d, snap.vix_spike, snap.curve_flip,
        snap.rates_shocked, len(snap.warnings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
