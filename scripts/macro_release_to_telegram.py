"""Macro release scanner — Mon-Fri 09:00 ET.

The fifteenth scheduled agent. Gates internally on
``release_calendar.get_release_today(today_iso)``; non-release days
exit silently with no archive write.

On release days:

- **CPI / PCE / NFP / GDP / PPI** → ``build_release_event`` runs the
  full template-fill pipe: FRED series → numeric transforms →
  summarizer (Layer 1 prompt + Layer 2 regex reject of predictive
  verbs + numeric leak). The card surfaces Python-computed numbers
  alongside the LLM's qualitative labels.

- **FOMC** → routes through ``fomc_parser`` (Python statement diff +
  SEP dot-plot extract) + ``tavily_consensus`` (sell-side hawkish /
  dovish majority vote restricted to 8 trusted news domains). The
  LLM is NEVER asked for a hawkish / dovish verdict per Stage 0
  design ack.

Priority is computed per release using ``surprise_sigma`` magnitude
and the FOMC SEP shift; see ``v2/reporting/priority.py`` for the
ladder.

⑮b FOMC +6h follow-up (transcript-based re-push) is deferred to
Phase 4.5 — needs its own Stage 0 work to choose a transcript source
and chunking strategy. The main ⑮ card already carries the statement
diff + dot plot + sell-side aggregate, which is the substance.

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
from v2.macro import build_release_event
from v2.macro.release_calendar import get_release_today
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

def _release_kind_and_meta(release) -> tuple[str, dict]:
    """Pick the priority kind for a non-FOMC release based on
    surprise_label, plus the metadata the priority module reads."""
    sigma = release.surprise_sigma or 0.0
    abs_sigma = abs(sigma)
    md = {"surprise_sigma": sigma, "surprise_label": release.surprise_label}

    if abs_sigma >= 3.0:
        return "macro_release_p0", md
    if abs_sigma >= 1.0:
        return "macro_release_p1", md
    return "macro_release_p2", md


def _fomc_kind_and_meta(event) -> tuple[str, dict]:
    """FOMC routing: SEP shift → P0; sell-side hawkish unexpected →
    extra nudge; otherwise base P1 (FOMC is always at least P1)."""
    md = {
        "is_fomc": True,
        "sep_shift": event.sep_dot_plot_change,
        "sell_side_consensus": (
            "hawkish_unexpected"
            if event.sell_side_sentiment == "hawkish"
            else event.sell_side_sentiment
        ),
    }
    if event.sep_dot_plot_change in ("hawkish_shift", "dovish_shift"):
        return "macro_release_p0", md
    return "macro_release_p1", md


# ---------------------------------------------------------------------------
# Inline card formatters (Stage 5 lift target)
# ---------------------------------------------------------------------------

def _fmt_pct(p: float | None, *, signed: bool = True) -> str:
    if p is None:
        return "—"
    sign = "+" if signed and p >= 0 else ""
    return f"{sign}{p:.2%}"


def _fmt_num(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}"


_TONE_EMOJI = {
    "hawkish": "🟥",
    "dovish":  "🟩",
    "neutral": "⚪",
}


def _format_release_card(release, tier: str) -> str:
    """Card body for a non-FOMC release (CPI / PCE / NFP / GDP / PPI / Claims)."""
    tone_emoji = _TONE_EMOJI.get(release.tone or "neutral", "⚪")

    lines: list[str] = [
        f"<b>📅 {release.release_type} · {release.period}</b>",
        f"发布日：<code>{release.release_date}</code> · 评级：<b>{tier}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # Numeric panel (Python-computed)
    if release.mom_pct is not None:
        lines.append(f"  MoM: <code>{_fmt_pct(release.mom_pct)}</code>")
    if release.yoy_pct is not None:
        lines.append(f"  YoY: <code>{_fmt_pct(release.yoy_pct)}</code>")
    if release.headline is not None:
        lines.append(f"  Headline: <code>{_fmt_num(release.headline)}</code>")
    if release.core is not None:
        lines.append(f"  Core: <code>{_fmt_num(release.core)}</code>")

    if release.consensus is not None:
        sigma_str = (
            f" ({release.surprise_sigma:+.1f}σ, {release.surprise_label})"
            if release.surprise_sigma is not None else ""
        )
        lines.append(
            f"  Consensus: <code>{_fmt_num(release.consensus)}</code>{sigma_str}"
        )
    lines.append(f"  3M 趋势: <i>{release.trailing_3mo_trend}</i>")

    # LLM qualitative panel (Layer 1+2 sanitized)
    if release.narrative:
        lines.append("")
        lines.append(f"<b>{tone_emoji} 解读</b> <i>({release.tone})</i>")
        lines.append(f"  {release.narrative}")
    if release.bull_takeaway:
        lines.append(f"  🟢 {release.bull_takeaway}")
    if release.bear_takeaway:
        lines.append(f"  🔴 {release.bear_takeaway}")

    return "\n".join(lines)


def _format_fomc_card(event, tier: str) -> str:
    """Card body for a FOMC event (Python diff + Tavily aggregate)."""
    lines: list[str] = [
        f"<b>🏛️ FOMC · {event.meeting_date}</b>",
        f"评级：<b>{tier}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # Statement diff
    diff = event.statement_diff or {}
    added = diff.get("added_phrases") or []
    removed = diff.get("removed_phrases") or []

    if added:
        lines.append("<b>📌 Statement 新增措辞</b>")
        for p in added[:6]:
            lines.append(f"  ➕ <i>{p}</i>")
    if removed:
        lines.append("<b>📌 Statement 移除措辞</b>")
        for p in removed[:6]:
            lines.append(f"  ➖ <i>{p}</i>")
    if not added and not removed:
        lines.append("<b>📌 Statement</b> <i>(关键措辞无变动)</i>")

    # SEP dots
    if event.has_sep:
        lines.append("")
        lines.append(f"<b>📊 SEP Dot Plot</b> <i>({event.sep_dot_plot_change})</i>")
        if event.sep_median_dots:
            for k, v in event.sep_median_dots.items():
                lines.append(f"  {k}: <code>{v:.2f}%</code>")
        else:
            lines.append("  <i>(数据待解析)</i>")

    # Sell-side aggregate
    if event.sell_side_sentiment:
        lines.append("")
        lines.append(
            f"<b>📰 卖方读数</b>: <i>{event.sell_side_sentiment}</i> "
            f"<i>(Tavily majority vote)</i>"
        )
        if event.sell_side_sources:
            sources_short = ", ".join(event.sell_side_sources[:4])
            lines.append(f"  来源: {sources_short}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------

def _push_release(notifier, trace, release) -> None:
    kind, md = _release_kind_and_meta(release)
    priority = compute_importance(kind, md)

    text = _format_release_card(release, tier=priority.tier)
    notifier.send_text(
        text,
        trace=trace,
        title=f"宏观 {release.release_type} · {release.period} · {priority.tier}",
        tickers=[],
        priority=priority,
    )


def _push_fomc(notifier, trace, fomc) -> None:
    kind, md = _fomc_kind_and_meta(fomc)
    priority = compute_importance(kind, md)

    text = _format_fomc_card(fomc, tier=priority.tier)
    notifier.send_text(
        text,
        trace=trace,
        title=f"FOMC · {fomc.meeting_date} · {priority.tier}",
        tickers=[],
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@notify_on_error("Macro Release Scanner")
def main() -> int:
    load_dotenv()
    install_all()

    today_iso = datetime.now(_TZ_ET).date().isoformat()
    todays = get_release_today(today_iso)

    if not todays:
        logger.info("Macro release: no scheduled release on %s — silent exit",
                    today_iso)
        return 0

    archive = Archive("macro")

    with capture_trace_with_framing(
        agent="macro", intent="macro_release_view",
        text=f"(自动推送) 宏观 release scanner · {today_iso} · "
             f"{len(todays)} 个 release",
        responder_name="_r_macro_release",
    ) as trace:
        report = build_release_event(today_iso)
        trace.emit(
            "chat_message", role="bot",
            text=(
                f"宏观 release · {today_iso} · "
                f"{len(report.today_releases)} releases · "
                f"FOMC={report.fomc_event is not None} · "
                f"warnings={len(report.warnings)}"
            ),
        )

        notifier = TelegramNotifier(archive=archive)

        # FOMC first if present (it's always the highest-priority event
        # of any FOMC day).
        if report.fomc_event is not None:
            try:
                _push_fomc(notifier, trace, report.fomc_event)
            except Exception as exc:
                logger.warning("FOMC push failed: %s", exc)

        for release in report.today_releases:
            try:
                _push_release(notifier, trace, release)
            except Exception as exc:
                logger.warning("Release push failed for %s: %s",
                               release.release_type, exc)

    logger.info(
        "Macro release complete: %d releases / FOMC=%s / warnings=%d",
        len(report.today_releases),
        report.fomc_event is not None,
        len(report.warnings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
