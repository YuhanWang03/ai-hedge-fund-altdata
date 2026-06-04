"""APScheduler entry — configure jobs and start the blocking scheduler.

When started, pushes a Telegram message listing each job's next run time
so you know it's alive.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

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
)

logger = logging.getLogger(__name__)

# All cron triggers run in US Eastern (where NYSE/NASDAQ live).
_TZ = ZoneInfo("US/Eastern")


def build_scheduler() -> BlockingScheduler:
    """Configure jobs without starting. Returns the scheduler ready to .start()."""
    scheduler = BlockingScheduler(timezone=_TZ)

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
