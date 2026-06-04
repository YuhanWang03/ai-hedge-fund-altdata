"""Unified push importance scoring.

Every notifier.send_text caller computes a PriorityResult and passes it
down so the notifier can:

  P0 (≥ 80)  immediate Telegram + 🚨🚨🚨 prefix + dashboard red chip
  P1 (60-79) immediate Telegram + dashboard blue chip
  P2 (40-59) archive only (rolled up by the daily digest cron)
  P3 (< 40)  archive only, dashboard hides by default

The base score for each kind of push is in BASE_SCORES; metadata
adjustments (price moves, holdings, surprise, etc.) compose on top.
Pure Python rules — no LLM involved.

Public surface used by callers:
  - compute_importance(event_kind, metadata) -> PriorityResult
  - tier_emoji_prefix(tier) -> str   (for Telegram message decoration)
  - tier_chip_color(tier)   -> str   (Tailwind class string for dashboard)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PriorityTier = Literal["P0", "P1", "P2", "P3"]


# Neutral base by event kind. Callers should use these stable keys
# (don't free-form). Unrecognized kinds fall back to "default" / P2.
BASE_SCORES: dict[str, int] = {
    "alert_fire":          85,  # user /alert triggered — always P0
    "intraday_anomaly":    65,  # mid-session anomaly — P1
    "anomaly_attribution": 60,  # post-close anomaly — base P1, adjustable
    "screen_result":       55,  # daily screen — P2 by default
    "lateral_expansion":   55,  # supply-chain expansion — P2
    "etf_daily":           50,  # ARK daily snapshot — P2
    "etf_significant":     75,  # ARK significant rebalance — P1
    "institutional_13f":   65,  # 13F push — P1
    "earnings_summary":    70,  # earnings recap — P1
    "earnings_reminder":   45,  # generic earnings reminder — P2 (legacy)
    "earnings_reminder_d3":45,  # D-3 reminder — P2
    "earnings_reminder_d1":60,  # D-1 reminder — P1 (upgraded urgency)
    "earnings_reminder_d0":60,  # D-0 release day — P1
    "earnings_pending":    45,  # FD hasn't ingested yet — P2 placeholder
    "portfolio_risk":      55,  # portfolio risk daily — P2 base
    "portfolio_alert":     85,  # portfolio drawdown / concentration — P0
    "macro_event":         70,  # macro data print — P1
    "macro_critical":      90,  # FOMC / CPI — P0
    "regime_change":       80,  # market regime shift — P0
    "sec_filing":          50,  # routine filing — P2
    "sec_critical":        85,  # 8-K material event — P0
    "scheduler_status":    30,  # scheduler startup message — P3
    "error_alert":         75,  # error reported by @notify_on_error — P1
    "p2_digest":           65,  # the digest itself is P1
    "default":             55,  # unrecognized → P2
}


@dataclass(frozen=True)
class PriorityResult:
    score: int
    tier: PriorityTier
    reasons: list[str]   # human-readable adjustment trail, for trace + debug


def compute_importance(
    event_kind: str,
    metadata: dict | None = None,
) -> PriorityResult:
    """Score a push from 0 to 100 and bucket it into P0–P3.

    metadata fields the scorer reads (all optional):
      - reasons_count: int           — Tavily-Verifier reasons that survived
      - flags: list[str]              — anomaly chips (e.g. "contrarian_move")
      - price_change_pct: float       — fraction, abs value matters
      - surprise_pct: float           — earnings surprise fraction
      - daily_pnl_pct: float          — portfolio daily PnL fraction (negative = loss)
      - is_held_position: bool        — ticker is in the user's broker account
      - is_watchlist: bool            — ticker is on the user's watchlist
      - action: str                   — ETF rebalance action ("exit", "buy", ...)
      - ticker_held_by_user: bool     — ETF rebalance touches a held position
      - sec_item: str                 — 8-K item code ("2.02", "5.02", ...)
    """
    md = metadata or {}
    base = BASE_SCORES.get(event_kind, BASE_SCORES["default"])
    adjustments: list[tuple[int, str]] = []

    # ---- anomaly attribution ----
    if event_kind == "anomaly_attribution":
        reasons = int(md.get("reasons_count") or 0)
        if reasons == 0:
            adjustments.append((-25, "no_tavily_reasons"))
        elif reasons >= 3:
            adjustments.append((+10, "rich_attribution"))

        flags = md.get("flags") or []
        if "contrarian_move" in flags:
            adjustments.append((+15, "contrarian"))

        pct = abs(float(md.get("price_change_pct") or 0.0))
        if pct >= 0.05:
            adjustments.append((+10, f"big_move_{pct:.2%}"))

    # ---- earnings ----
    if event_kind == "earnings_summary":
        surprise = abs(float(md.get("surprise_pct") or 0.0))
        if surprise >= 0.10:
            # +30 puts even a no-holdings BEAT/MISS firmly into P0
            # (70 base → 100 capped). Big surprises are the whole reason
            # to wake the user at 21:00 ET.
            adjustments.append((+30, f"big_surprise_{surprise:.1%}"))
        if md.get("guidance_lowered"):
            adjustments.append((+10, "guidance_lowered"))

    # ---- portfolio ----
    if event_kind == "portfolio_risk":
        pnl_pct = float(md.get("daily_pnl_pct") or 0.0)
        if pnl_pct <= -0.05:
            adjustments.append((+30, f"daily_loss_{pnl_pct:.1%}"))  # → P0
        elif pnl_pct <= -0.02:
            adjustments.append((+10, "moderate_loss"))

    # ---- universal: holdings / watchlist matter for everything ----
    if md.get("is_held_position"):
        adjustments.append((+15, "held_position"))
    elif md.get("is_watchlist"):
        adjustments.append((+10, "watchlist"))

    # ---- ETF significant rebalance ----
    if event_kind == "etf_significant":
        if md.get("action") == "exit":
            adjustments.append((+10, "etf_exit"))
        if md.get("ticker_held_by_user"):
            adjustments.append((+15, "etf_touches_holding"))

    # ---- SEC 8-K item codes ----
    if event_kind == "sec_critical":
        item = str(md.get("sec_item") or "")
        if item.startswith("2."):
            adjustments.append((+5, "earnings_item"))
        elif item.startswith("5."):
            adjustments.append((+5, "exec_change"))

    raw = base + sum(d for d, _ in adjustments)
    score = max(0, min(100, raw))
    tier: PriorityTier = (
        "P0" if score >= 80 else
        "P1" if score >= 60 else
        "P2" if score >= 40 else
        "P3"
    )

    reasons_log = [f"base={base}"] + [
        f"{d:+d}_{label}" for d, label in adjustments
    ]
    return PriorityResult(score=score, tier=tier, reasons=reasons_log)


def tier_emoji_prefix(tier: PriorityTier) -> str:
    """Telegram message prefix for visual emphasis.

    P0 gets a 🚨 cluster so it's unmistakable on the lock screen.
    P1 stays clean — most pushes are P1, prefixes would dilute.
    P2 prefix only ever shows up on the daily digest header itself.
    P3 never reaches Telegram so its prefix is moot.
    """
    return {
        "P0": "🚨🚨🚨 ",
        "P1": "",
        "P2": "📋 ",
        "P3": "",
    }[tier]


def tier_chip_color(tier: PriorityTier) -> str:
    """Tailwind class string for the dashboard's priority chip."""
    return {
        "P0": "bg-rose-500 text-white",
        "P1": "bg-blue-500 text-white",
        "P2": "bg-amber-400 text-amber-900",
        "P3": "bg-slate-300 text-slate-600",
    }[tier]


__all__ = [
    "PriorityTier",
    "PriorityResult",
    "BASE_SCORES",
    "compute_importance",
    "tier_emoji_prefix",
    "tier_chip_color",
]
