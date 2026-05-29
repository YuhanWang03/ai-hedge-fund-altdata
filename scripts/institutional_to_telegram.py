"""Run institutional 13F tracking and push to Telegram.

Usage:
    poetry run python scripts/institutional_to_telegram.py
"""

from __future__ import annotations

import logging
import time

from dotenv import load_dotenv

from v2.archive import Archive
from v2.institutional import run_institutional_pipeline
from v2.reporting import (
    TelegramNotifier,
    format_institutional_messages,
    notify_on_error,
)
from v2.screening import TECH_30

logging.basicConfig(level=logging.INFO, format="  [%(levelname)s] %(message)s")

load_dotenv()


@notify_on_error("Institutional 13F")
def main() -> None:
    print("Running institutional 13F pipeline...")
    report = run_institutional_pipeline(universe=set(TECH_30))

    print(
        f"\nDone. {len(report.new_filings)} new filings · "
        f"{len(report.changes)} significant changes"
    )

    if not report.new_filings:
        print("No new 13F filings since last run — staying silent.")
        return

    messages = format_institutional_messages(report)
    print(f"Pushing {len(messages)} messages to Telegram...")

    notifier = TelegramNotifier(archive=Archive(agent="institutional"))
    for i, msg in enumerate(messages, 1):
        notifier.send_text(msg)
        print(f"  [{i}/{len(messages)}] sent")
        if i < len(messages):
            time.sleep(0.3)   # gentle pacing to avoid rate limit spikes

    print("Pushed.")


if __name__ == "__main__":
    main()
