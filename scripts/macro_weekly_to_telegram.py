"""Macro weekly recap — Fri 19:30 ET.

The seventeenth scheduled agent. Pinned at 19:30 ET by Stage 0 design
to clear ⑨ Portfolio Risk (18:30) and ⑩ Portfolio Weekly (19:00).

Recap content:
- This-week releases that already fired (from release_calendar window)
- Next-week schedule preview
- 1W deltas on VIX / DGS10 / DGS2 / T10Y2Y (Python-computed; no LLM)

Priority is always ``macro_weekly`` (P1 floor). Same posture as
⑩ Portfolio Weekly — operator visibility is the point, not
event-driven escalation. Mid-week shock signals come from ⑭/⑮ instead.

Card formatter is inline (Stage 5 lifts to v2.reporting.format_macro_weekly).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from v2.archive import Archive
from v2.macro import build_weekly_recap
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
# Inline card formatter
# ---------------------------------------------------------------------------

_DELTA_LABELS = {
    "VIXCLS": "VIX",
    "DGS10":  "10Y",
    "DGS2":   "2Y",
    "T10Y2Y": "10Y-2Y",
}


def _fmt_delta(v: float | None, *, places: int = 2) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.{places}f}"


def _format_weekly_card(recap: dict) -> str:
    week_start = recap.get("week_start", "—")
    week_end = recap.get("week_end", "—")

    lines: list[str] = [
        f"<b>📊 宏观周报 · {week_start} → {week_end}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # Weekly deltas
    deltas = recap.get("weekly_deltas") or {}
    lines.append("<b>本周变化</b>")
    for sid, label in _DELTA_LABELS.items():
        d = deltas.get(sid)
        lines.append(f"  {label}: <code>{_fmt_delta(d)}</code>")

    # This-week fired releases
    this_week = recap.get("this_week_releases") or {}
    lines.append("")
    if this_week:
        lines.append("<b>本周已发布</b>")
        for iso in sorted(this_week.keys()):
            entries = this_week[iso]
            labels = " / ".join(rel_type for rel_type, _, _ in entries)
            lines.append(f"  <code>{iso}</code>: {labels}")
    else:
        lines.append("<i>本周无 release 触发</i>")

    # Next-week preview
    next_week = recap.get("next_week_releases") or {}
    lines.append("")
    if next_week:
        lines.append("<b>下周预告</b>")
        for iso in sorted(next_week.keys()):
            entries = next_week[iso]
            labels = " / ".join(rel_type for rel_type, _, _ in entries)
            lines.append(f"  <code>{iso}</code>: {labels}")
    else:
        lines.append("<i>下周无重大 release</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("Macro Weekly Recap")
def main() -> int:
    load_dotenv()
    install_all()

    today_iso = datetime.now(_TZ_ET).date().isoformat()
    archive = Archive("macro")

    with capture_trace_with_framing(
        agent="macro", intent="macro_weekly_view",
        text=f"(自动推送) 宏观周报 · {today_iso}",
        responder_name="_r_macro_weekly",
    ) as trace:
        recap = build_weekly_recap(today_iso)
        trace.emit(
            "chat_message", role="bot",
            text=(
                f"宏观周报 · {today_iso} · "
                f"this_week={len(recap.get('this_week_releases') or {})} · "
                f"next_week={len(recap.get('next_week_releases') or {})}"
            ),
        )

        priority = compute_importance("macro_weekly", {})
        text = _format_weekly_card(recap)

        notifier = TelegramNotifier(archive=archive)
        try:
            notifier.send_text(
                text,
                trace=trace,
                title=f"宏观周报 · {today_iso} · {priority.tier}",
                tickers=[],
                priority=priority,
            )
        except Exception as exc:
            logger.warning("Weekly recap push failed: %s", exc)
            return 1

    logger.info(
        "Macro weekly complete: tier=%s this_week=%d next_week=%d",
        priority.tier,
        len(recap.get('this_week_releases') or {}),
        len(recap.get('next_week_releases') or {}),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
