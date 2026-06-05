"""Scheduled job definitions.

Each job spawns the corresponding entry script as a subprocess. Two reasons:
1. Process isolation — if one agent crashes, the others keep working.
2. Reuses the existing scripts unchanged (and their @notify_on_error decorators).

The scheduler doesn't need to know anything about FD / DeepSeek / Telegram —
that's all encapsulated in the scripts.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"


def _run(script_name: str) -> None:
    """Run a script in a subprocess. Errors are pushed to Telegram by the
    script itself via @notify_on_error — we just log the exit code here.
    """
    script_path = _SCRIPTS_DIR / script_name
    logger.info("Triggering %s", script_name)
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode == 0:
        logger.info("Finished %s (exit 0)", script_name)
    else:
        logger.warning("%s exited %d (Telegram alert already sent)",
                       script_name, result.returncode)


def daily_screen_job() -> None:
    _run("daily_screen_to_telegram.py")


def anomaly_monitor_job() -> None:
    _run("anomaly_to_telegram.py")


def lateral_expansion_job() -> None:
    _run("lateral_to_telegram.py")


def institutional_job() -> None:
    _run("institutional_to_telegram.py")


def institutional_backfill_job() -> None:
    """Weekly: re-fetch ALL tracked managers (idempotent), even unchanged ones.

    Why: keeps edgar.db internally consistent if the source 13F data is
    revised, and catches managers whose newest filing was missed by the
    Tue/Fri agent (e.g. amended filings, race conditions).
    """
    _run("backfill_13f.py")


def etf_daily_job() -> None:
    _run("etf_daily_snapshot.py")


def earnings_reminders_job() -> None:
    """⑦ Daily 08:00 ET — calendar reminders (D-3 / D-1 / D-0)."""
    _run("earnings_reminders.py")


def earnings_summaries_job() -> None:
    """⑧ Daily 21:00 ET — post-release summaries with FD-pending retry."""
    _run("earnings_summaries.py")


def portfolio_risk_job() -> None:
    """⑨ Daily 18:30 ET — portfolio risk snapshot (concentration / P&L /
    drawdown / earnings density)."""
    _run("portfolio_risk_to_telegram.py")


def portfolio_weekly_job() -> None:
    """⑩ Fri 19:00 ET — weekly recap with equity-curve chart."""
    _run("portfolio_weekly_to_telegram.py")


def sec_8k_job() -> None:
    """⑪ Mon-Fri 17:05 ET — SEC 8-K scanner for held + watchlist universe."""
    _run("sec_8k_to_telegram.py")


def sec_form4_job() -> None:
    """⑫ Mon-Fri 17:45 ET — SEC Form 4 (insider transactions) scanner.

    Time slot chosen to avoid collision with ① Daily Screen (17:30 ET)
    and ② Anomaly Monitor (17:35 ET). 17:45 ET sits cleanly between
    them and the 18:00 ET ③/④ block.
    """
    _run("sec_form4_to_telegram.py")


def macro_daily_snapshot_job() -> None:
    """⑭ Mon-Fri 16:30 ET — Macro daily snapshot.

    Post-close: VIX / DXY / WTI / Gold (yfinance) + Fed Funds / 2Y /
    10Y / T10Y2Y (FRED canonical). 16:30 ET slot deliberately sits
    15 min before the 16:45 ET P2 digest and 30 min before the 17:00 ET
    cron block — no collision with the rest of the schedule.
    """
    _run("macro_daily_snapshot.py")


def macro_release_job() -> None:
    """⑮ Mon-Fri 09:00 ET — Macro release scanner.

    Gates internally on release_calendar.get_release_today(today). On
    release days routes CPI/PCE/NFP/GDP/PPI through summarizer (Layer
    1+2 defense) and FOMC through fomc_parser + tavily_consensus
    (Python diff + sell-side majority vote — never an LLM hawkish
    verdict).
    """
    _run("macro_release_to_telegram.py")


def macro_claims_job() -> None:
    """⑯ Thu 09:30 ET — Initial Jobless Claims.

    Thursday-only trigger is the deterministic gate (no calendar lookup
    needed — ICSA releases every Thursday 08:30 ET). build_claims_event
    surfaces the weekly print + 4-week MA smoothed level.
    """
    _run("macro_claims_to_telegram.py")


def macro_weekly_job() -> None:
    """⑰ Fri 19:30 ET — Macro weekly recap.

    19:30 slot picked to clear ⑨ 18:30 / ⑩ 19:00 (Stage 0 macro spec
    requirement). Recap = this-week fired releases + next-week
    schedule + 1W deltas on VIX / DGS10 / DGS2 / T10Y2Y.
    """
    _run("macro_weekly_to_telegram.py")


def p2_digest_job() -> None:
    """Daily roll-up of P2-tier pushes — runs 16:45 ET before the
    17:00–18:30 ET cron block."""
    _run("p2_digest_to_telegram.py")


def archive_cleanup_job() -> None:
    """Daily sweep — remove archive rows past their expires_at watermark.

    The dashboard auto-push feed retains 2 calendar days of pushes (set by
    TelegramNotifier when it writes the row). Without this sweep the
    archive grows unbounded. Runs at 02:00 UTC so we never collide with
    the 17:00–18:30 ET cron block.
    """
    import sqlite3
    from datetime import datetime, timezone
    db = _PROJECT_ROOT / "data" / "archive.db"
    if not db.exists():
        logger.info("archive_cleanup: no archive.db yet, skipping")
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db), timeout=10.0)
    try:
        cur = conn.execute(
            "DELETE FROM pushes WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now_iso,),
        )
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    logger.info("archive_cleanup: deleted %d expired rows", deleted)
