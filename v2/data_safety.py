"""FD data-safety helpers.

Live at v2/data_safety.py rather than v2/data/helpers.py so the sandbox
(no v2/data/) can host these — the production VPS has the same import
path either way because v2/data_safety.py is just a top-level module.

The single helper here, fd_safe_today(), addresses the production bug
where v2 callers asked FD for prices "up to today" but FD's data
coverage lags real-world time by 1-7 days. Requesting `end_date` past
FD's coverage window yields HTTP 400 → empty list → cascading
"No price data" failures across every responder / cron agent.

Defense layer 1 (this file): every call site that previously did
`today = date.today()` should switch to `fd_safe_today()`. The default
3-day buffer accommodates typical end-of-week + holiday gaps without
sacrificing recency too much.

Defense layer 2 (recommended in v2/data/client.py — patch shipped in
this commit's message): FDClient.get_prices() should also catch the
HTTP 400 and auto-rollback end_date by 7 → 14 → 30 days before giving
up, so a misconfigured caller still gets useful data.
"""

from __future__ import annotations

from datetime import date, timedelta


def fd_safe_today(buffer_days: int = 3) -> date:
    """Return today() - buffer_days, intended as the `end_date` for any
    fd.get_prices(...) call.

    Why 3 by default: financialdatasets.ai data updates with a typical
    lag of 1 day on weekdays and up to 3 calendar days across weekends.
    A buffer of 3 covers Monday morning queries reading Friday's close
    without falling off the coverage edge.

    Callers that need stricter recency (e.g. intraday after-hours queries
    on a known business day) can pass buffer_days=1. Callers that need
    extra safety (e.g. holidays) can pass buffer_days=7.
    """
    return date.today() - timedelta(days=buffer_days)


__all__ = ["fd_safe_today"]
