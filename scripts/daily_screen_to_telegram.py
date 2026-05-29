"""Run the daily tech-stock screen, narrate with DeepSeek, push to Telegram.

Usage:
    poetry run python scripts/daily_screen_to_telegram.py
"""

from __future__ import annotations

from dotenv import load_dotenv

from v2.archive import Archive
from v2.data import CachedFDClient
from v2.reporting import TelegramNotifier, format_screening_result, notify_on_error
from v2.screening import DEFAULT_FILTERS, TECH_30, narrate, run_screening

load_dotenv()


@notify_on_error("Daily Screen")
def main() -> None:
    print(f"Scanning {len(TECH_30)} tickers...")
    with CachedFDClient() as fd:
        result = run_screening(TECH_30, fd, DEFAULT_FILTERS)

    print(f"Passed: {len(result.candidates)}/{result.universe_size}")

    if result.candidates:
        print("Narrating with DeepSeek...")
        narrations, tokens = narrate(result.candidates)
        result.llm_tokens = tokens
        for c in result.candidates:
            note = narrations.get(c.ticker, {})
            c.bull = note.get("bull", "") or ""
            c.bear = note.get("bear", "") or ""

    notifier = TelegramNotifier(archive=Archive(agent="screen"))
    notifier.send_text(format_screening_result(result))
    print("Done.")


if __name__ == "__main__":
    main()
