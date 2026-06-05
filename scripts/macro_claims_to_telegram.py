"""Macro Initial Jobless Claims — Thu 09:30 ET.

The sixteenth scheduled agent. ICSA (Initial Claims) prints every
Thursday at 08:30 ET; this cron pulls FRED's canonical series at
09:30 ET (giving the BLS feed ~1 hour to land in FRED) and pushes
a single card with:

- Latest weekly print
- 4-week MA smoothed level (the trend operators actually read)
- 3-month trailing direction label (accelerating / decelerating / flat)
- LLM template-fill qualitative labels (Layer 1+2 sanitized)

Holiday weeks (Thanksgiving / year-end) where BLS shifts publication
days → the FRED series omits the latest week → the pipeline returns
no headline → cron logs and exits silently.

Default priority is ``macro_release_p2`` (P2). Surprises ≥ 2σ bump
to P1 via the standard ``macro_release_p1`` path.

Card formatter shares ``_format_release_card`` with the ⑮ scanner
(re-imported here for Stage 2; Stage 5 lifts both crons to the same
public formatter).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from v2.archive import Archive
from v2.macro import build_claims_event
from v2.observability import capture_trace_with_framing, install_all
from v2.reporting import TelegramNotifier, notify_on_error
from v2.reporting.priority import compute_importance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


_TZ_ET = ZoneInfo("US/Eastern")


# Inline card formatter — shared shape with ⑮ release. Stage 5 will
# lift to v2.reporting.format_macro_release.
def _fmt_num(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}"


_TONE_EMOJI = {
    "hawkish": "🟥",
    "dovish":  "🟩",
    "neutral": "⚪",
}


def _format_claims_card(release, tier: str) -> str:
    tone_emoji = _TONE_EMOJI.get(release.tone or "neutral", "⚪")
    lines: list[str] = [
        f"<b>📅 Initial Claims · {release.period}</b>",
        f"发布日：<code>{release.release_date}</code> · 评级：<b>{tier}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"  本周: <code>{_fmt_num(release.headline)}</code>",
        f"  4W MA: <code>{_fmt_num(release.core)}</code> "
        f"<i>(smoothed)</i>",
        f"  上周值: <code>{_fmt_num(release.prior_value)}</code>",
        f"  3M 趋势: <i>{release.trailing_3mo_trend}</i>",
    ]
    if release.narrative:
        lines.append("")
        lines.append(f"<b>{tone_emoji} 解读</b> <i>({release.tone})</i>")
        lines.append(f"  {release.narrative}")
    if release.bull_takeaway:
        lines.append(f"  🟢 {release.bull_takeaway}")
    if release.bear_takeaway:
        lines.append(f"  🔴 {release.bear_takeaway}")
    return "\n".join(lines)


def _kind_and_meta(release) -> tuple[str, dict]:
    sigma = release.surprise_sigma or 0.0
    md = {"surprise_sigma": sigma, "surprise_label": release.surprise_label}
    if abs(sigma) >= 2.0:
        return "macro_release_p1", md
    return "macro_release_p2", md


@notify_on_error("Macro Initial Claims")
def main() -> int:
    load_dotenv()
    install_all()

    today_iso = datetime.now(_TZ_ET).date().isoformat()
    archive = Archive("macro")

    with capture_trace_with_framing(
        agent="macro", intent="macro_claims_view",
        text=f"(自动推送) Initial Claims · {today_iso}",
        responder_name="_r_macro_claims",
    ) as trace:
        release = build_claims_event(today_iso)
        if release is None or release.headline is None:
            logger.info(
                "Macro claims: no ICSA data for %s "
                "(holiday week or FRED lag) — silent skip", today_iso,
            )
            return 0

        trace.emit(
            "chat_message", role="bot",
            text=(
                f"Initial Claims · {today_iso} · headline={release.headline} · "
                f"4WMA={release.core} · trend={release.trailing_3mo_trend}"
            ),
        )

        kind, md = _kind_and_meta(release)
        priority = compute_importance(kind, md)

        text = _format_claims_card(release, tier=priority.tier)
        notifier = TelegramNotifier(archive=archive)
        try:
            notifier.send_text(
                text,
                trace=trace,
                title=f"Initial Claims · {today_iso} · {priority.tier}",
                tickers=[],
                priority=priority,
            )
        except Exception as exc:
            logger.warning("Claims push failed: %s", exc)
            return 1

    logger.info(
        "Macro claims complete: weekly=%s 4WMA=%s tier=%s",
        release.headline, release.core, priority.tier,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
