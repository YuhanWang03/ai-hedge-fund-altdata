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
