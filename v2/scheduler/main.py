"""APScheduler entry — configure jobs and start the blocking scheduler.

When started, pushes a Telegram message listing each job's next run time
so you know it's alive.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from v2.scheduler.jobs import (
    anomaly_monitor_job,
    archive_cleanup_job,
    daily_screen_job,
    earnings_reminders_job,
    earnings_summaries_job,
    etf_daily_job,
    institutional_backfill_job,
    institutional_job,
    lateral_expansion_job,
    p2_digest_job,
    macro_claims_job,
    macro_daily_snapshot_job,
    macro_release_job,
    macro_weekly_job,
    portfolio_risk_job,
    portfolio_weekly_job,
    positions_snapshot_job,
    sec_8k_job,
    sec_form4_job,
)

logger = logging.getLogger(__name__)

# All cron triggers run in US Eastern (where NYSE/NASDAQ live).
_TZ = ZoneInfo("US/Eastern")


def build_scheduler() -> BlockingScheduler:
    """Configure jobs without starting. Returns the scheduler ready to .start().

    Default APScheduler ``ThreadPoolExecutor`` uses ``max_workers=10``.
    With 14 jobs (Phase 3 added ⑪ + ⑫) and Mon-Fri 17:00-21:00 ET burst
    window, a server restart catching up multiple misfires could saturate
    10 workers. Bumped to 20 — costs near zero, comfortable headroom.
    """
    scheduler = BlockingScheduler(
        timezone=_TZ,
        executors={"default": ThreadPoolExecutor(max_workers=20)},
    )

    scheduler.add_job(
        daily_screen_job,
        CronTrigger(hour=17, minute=30, day_of_week="mon-fri"),
        id="daily_screen",
        name="① Daily Screen",
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.add_job(
        anomaly_monitor_job,
        CronTrigger(hour=17, minute=35, day_of_week="mon-fri"),
        id="anomaly_monitor",
        name="② Anomaly Monitor",
        misfire_grace_time=3600,
        coalesce=True,
    )

    scheduler.add_job(
        lateral_expansion_job,
        CronTrigger(hour=18, minute=0, day_of_week="mon"),
        id="lateral_expansion",
        name="③ Lateral Expansion (weekly)",
        misfire_grace_time=7200,
        coalesce=True,
    )

    scheduler.add_job(
        institutional_job,
        CronTrigger(hour=18, minute=0, day_of_week="tue,fri"),
        id="institutional",
        name="④ Institutional 13F (Tue/Fri)",
        misfire_grace_time=7200,
        coalesce=True,
    )

    scheduler.add_job(
        institutional_backfill_job,
        CronTrigger(hour=18, minute=30, day_of_week="sun"),
        id="institutional_backfill",
        name="④b 13F Backfill (Sun)",
        misfire_grace_time=7200,
        coalesce=True,
    )

    scheduler.add_job(
        etf_daily_job,
        CronTrigger(hour=17, minute=0, day_of_week="mon-fri"),
        id="etf_daily",
        name="⑤ ETF Daily Snapshot (Mon-Fri)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑦ Earnings reminders — 08:00 ET Mon-Fri. Pulls watchlist + Alpaca
    # holdings, asks yfinance for the next release per ticker, and pushes
    # one card for each that lands in D-3 / D-1 / D-0.
    scheduler.add_job(
        earnings_reminders_job,
        CronTrigger(hour=8, minute=0, day_of_week="mon-fri", timezone=_TZ),
        id="earnings_reminders",
        name="⑦ Earnings Reminders (Mon-Fri 08:00 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑧ Earnings summaries — 21:00 ET Mon-Fri. Tickers whose calendar said
    # "release today" get the post-release card if FD has the actuals; a
    # short pending placeholder otherwise (retried by the next 21:00 ET run).
    scheduler.add_job(
        earnings_summaries_job,
        CronTrigger(hour=21, minute=0, day_of_week="mon-fri", timezone=_TZ),
        id="earnings_summaries",
        name="⑧ Earnings Summaries (Mon-Fri 21:00 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑨ Portfolio risk — 18:30 ET Mon-Fri. Daily snapshot:
    # positions / concentration / sector exposure / P&L / drawdown /
    # 7-day held-position earnings risk. Computes priority from the
    # report itself; daily loss ≥ 5% bumps to P0.
    scheduler.add_job(
        portfolio_risk_job,
        CronTrigger(hour=18, minute=30, day_of_week="mon-fri", timezone=_TZ),
        id="portfolio_risk",
        name="⑨ Portfolio Risk (Mon-Fri 18:30 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑩ Portfolio weekly recap — 19:00 ET Fri. Weekly + monthly returns,
    # 1M drawdown, equity-curve PNG. Always P1 (operator-visibility) —
    # mid-week P0 risks come from ⑨ instead.
    scheduler.add_job(
        portfolio_weekly_job,
        CronTrigger(hour=19, minute=0, day_of_week="fri", timezone=_TZ),
        id="portfolio_weekly",
        name="⑩ Portfolio Weekly (Fri 19:00 ET)",
        misfire_grace_time=7200,
        coalesce=True,
    )

    # ⑪ SEC 8-K scanner — 17:05 ET Mon-Fri. 5 min after ⑤ ETF Daily so
    # the two HTTP-heavy jobs don't fight for connection pool. Items
    # classified P0-P3 by Stage-0 priority table; 2.02-only earnings
    # filings skipped (⑧ Earnings Summaries handles them at 21:00 ET).
    scheduler.add_job(
        sec_8k_job,
        CronTrigger(hour=17, minute=5, day_of_week="mon-fri", timezone=_TZ),
        id="sec_8k",
        name="⑪ SEC 8-K Scanner (Mon-Fri 17:05 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑫ SEC Form 4 scanner — 17:45 ET Mon-Fri. Slotted between
    # ② Anomaly Monitor (17:35) and ③/④ at 18:00 to avoid collision
    # with ① Daily Screen (17:30) which the Stage-2 prompt didn't
    # account for. P/S individual cards + same-day cluster cards;
    # A/M/F/G/C noise codes archived for Phase 3.5 weekly digest.
    scheduler.add_job(
        sec_form4_job,
        CronTrigger(hour=17, minute=45, day_of_week="mon-fri", timezone=_TZ),
        id="sec_form4",
        name="⑫ SEC Form 4 Scanner (Mon-Fri 17:45 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # P2 digest — 16:45 ET, just before the 17:00 cron block. Sweeps
    # the previous day's P2 archive rows into one Telegram message.
    scheduler.add_job(
        p2_digest_job,
        CronTrigger(hour=16, minute=45, day_of_week="mon-fri"),
        id="p2_digest",
        name="📋 P2 Digest (Mon-Fri 16:45 ET)",
        misfire_grace_time=1800,
        coalesce=True,
    )

    # ⑨b Positions snapshot — 16:25 ET Mon-Fri. Silent backend job:
    # writes one row per holding to positions_snapshot table so ⑩ Fri
    # 19:00 ET can compute per-position weekly attribution. No Telegram
    # push. 5 min before ⑭ Macro Snapshot — strictly serial under the
    # scheduler thread pool keeps log timestamps unambiguous when ops
    # grep. Captures EOD positions after the 16:00 close (Alpaca
    # account state is final by 16:15 even with after-hours feed lag).
    scheduler.add_job(
        positions_snapshot_job,
        CronTrigger(hour=16, minute=25, day_of_week="mon-fri", timezone=_TZ),
        id="positions_snapshot",
        name="⑨b Positions Snapshot (Mon-Fri 16:25 ET)",
        misfire_grace_time=600,
        coalesce=True,
    )

    # ⑭ Macro daily snapshot — 16:30 ET Mon-Fri. Post-close ambient
    # snapshot: VIX (yfinance) + DXY / WTI / Gold + Fed Funds / 2Y /
    # 10Y / T10Y2Y (FRED). 4 pinned anomaly flags (vix_spike +20% /
    # vix_elevated +10% / curve_flip / rates_shocked ≥20bps) bump
    # priority via the kind enum (macro_vix_spike / macro_curve_flip
    # / macro_snapshot_p3). 15 min before ⑥ P2 digest so the snapshot
    # row can be picked up by the digest if it lands at P2.
    scheduler.add_job(
        macro_daily_snapshot_job,
        CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=_TZ),
        id="macro_snapshot",
        name="⑭ Macro Daily Snapshot (Mon-Fri 16:30 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑮ Macro release scanner — 09:00 ET Mon-Fri. Gates internally on
    # release_calendar.get_release_today(today_iso); non-release days
    # exit silently with no archive write. On release days routes
    # CPI/PCE/NFP/GDP/PPI through summarizer (Layer 1+2 defense) and
    # FOMC through fomc_parser + tavily_consensus (never an LLM
    # verdict for hawkish/dovish).
    scheduler.add_job(
        macro_release_job,
        CronTrigger(hour=9, minute=0, day_of_week="mon-fri", timezone=_TZ),
        id="macro_release",
        name="⑮ Macro Release Scanner (Mon-Fri 09:00 ET)",
        misfire_grace_time=1800,
        coalesce=True,
    )

    # ⑯ Macro Initial Claims — Thu 09:30 ET. Deterministic Thursday
    # cadence; no calendar lookup. build_claims_event surfaces the
    # weekly print + 4-week MA smoothed level. Holiday weeks (Thanks-
    # giving / year-end shift) where ICSA omits a print → silent skip.
    scheduler.add_job(
        macro_claims_job,
        CronTrigger(hour=9, minute=30, day_of_week="thu", timezone=_TZ),
        id="macro_claims",
        name="⑯ Macro Initial Claims (Thu 09:30 ET)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # ⑰ Macro weekly recap — Fri 19:30 ET. Slot pinned by Stage 0
    # design to clear ⑨ 18:30 / ⑩ 19:00. This-week fired releases
    # + next-week schedule + 1W deltas on VIX / DGS10 / DGS2 / T10Y2Y.
    # Always P1 floor (operator visibility — same posture as ⑩).
    scheduler.add_job(
        macro_weekly_job,
        CronTrigger(hour=19, minute=30, day_of_week="fri", timezone=_TZ),
        id="macro_weekly",
        name="⑰ Macro Weekly Recap (Fri 19:30 ET)",
        misfire_grace_time=7200,
        coalesce=True,
    )

    # ⑥ Archive cleanup — sweep dashboard-feed rows past their 2-day TTL.
    # 02:00 UTC chosen to avoid the 17:00–18:30 ET push window.
    scheduler.add_job(
        archive_cleanup_job,
        CronTrigger(hour=2, minute=0),
        id="archive_cleanup",
        name="⑥ Archive Cleanup",
        misfire_grace_time=3600,
        coalesce=True,
    )

    return scheduler


def run_scheduler(test_now: bool = False) -> None:
    """Start the scheduler. Blocks until Ctrl+C.

    If *test_now* is True, run each job once immediately then exit —
    useful for verifying everything's wired up correctly.
    """
    scheduler = build_scheduler()

    if test_now:
        logger.info("Test mode: running all jobs once for verification...")
        for job in scheduler.get_jobs():
            logger.info("--- Running %s ---", job.name)
            job.func()
        logger.info("All jobs done. Exiting test mode.")
        return

    # BlockingScheduler doesn't populate Job.next_run_time until start() is
    # called — but start() blocks. Compute upcoming fires directly from each
    # trigger so we can announce them before the loop begins.
    now = datetime.now(_TZ)
    job_info: list[tuple[str, datetime | None]] = []
    for job in scheduler.get_jobs():
        try:
            next_fire = job.trigger.get_next_fire_time(None, now)
        except Exception:
            next_fire = None
        job_info.append((job.name, next_fire))

    _push_startup_message(job_info)

    logger.info("Scheduler started. Jobs:")
    for name, next_fire in job_info:
        next_str = (
            next_fire.strftime("%Y-%m-%d %H:%M 美东") if next_fire else "—"
        )
        logger.info("  • %s — next: %s", name, next_str)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down")


def _push_startup_message(job_info: list[tuple[str, datetime | None]]) -> None:
    """Send a Telegram message with the list of next run times — best effort."""
    try:
        # Local import — we don't want scheduler module to require Telegram env
        from v2.reporting import TelegramNotifier

        notifier = TelegramNotifier()
        lines: list[str] = ["<b>🤖 Scheduler 已启动</b>", ""]
        for name, next_fire in job_info:
            next_str = (
                next_fire.strftime("%Y-%m-%d %H:%M 美东") if next_fire else "—"
            )
            lines.append(f"• {name}")
            lines.append(f"  下次: <code>{next_str}</code>")
        notifier.send_text("\n".join(lines))
    except Exception as exc:
        logger.warning("Failed to push startup notification: %s", exc)
