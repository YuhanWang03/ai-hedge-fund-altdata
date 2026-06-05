"""FRED client wrapper — Phase 4 Stage 1.

Light wrapper around ``fredapi.Fred`` with:

- Lazy init (raises :class:`FredUnavailable` when ``FRED_API_KEY`` is
  not set in env — same pattern as the v2.broker AlpacaUnavailable
  guard in Phase 2).
- Bounded retry on transient HTTP errors (3 attempts, 1s linear
  backoff). FRED occasionally 502s mid-day; the cron path treats a
  failed series as a warning, not a fatal.
- Test seam via ``client_factory`` so unit tests can inject a fake
  Fred object.

This module does NOT cache results. The cron pulls fresh data on each
run; the dashboard backend has its own cache layer for repeated bot
queries.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

try:
    from fredapi import Fred
except ImportError:                                  # sandbox path
    Fred = None                                       # type: ignore[assignment]


logger = logging.getLogger(__name__)


_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SEC = 1.0


class FredUnavailable(RuntimeError):
    """Raised when FRED_API_KEY is missing or fredapi failed to install."""


def _build_default_client() -> "Fred":
    if Fred is None:
        raise FredUnavailable("fredapi package not installed")
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise FredUnavailable("FRED_API_KEY not set in env")
    return Fred(api_key=key)


def _with_retry(fn: Callable, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` with linear backoff. Raises the last
    exception if all attempts fail."""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                      # noqa: BLE001
            last_exc = exc
            if attempt + 1 < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SEC * (attempt + 1))
                logger.info(
                    "FRED retry %d/%d after %s: %s",
                    attempt + 1, _RETRY_ATTEMPTS,
                    type(exc).__name__, exc,
                )
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_series(
    series_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    client_factory: Callable[[], "Fred"] | None = None,
):
    """Pull a FRED series. Returns a pandas.Series (the raw fredapi
    return shape — index is date, values are floats).

    ``client_factory`` is a test seam. Default behavior uses the real
    Fred client built from the env var.
    """
    factory = client_factory or _build_default_client
    fred = factory()
    return _with_retry(
        fred.get_series, series_id,
        observation_start=start, observation_end=end,
    )


def get_release_dates(
    release_id: int,
    *,
    start: str,
    end: str,
    include_release_dates_with_no_data: bool = True,
    client_factory: Callable[[], "Fred"] | None = None,
) -> list:
    """Pull the schedule for a FRED release ID. Used by
    :mod:`v2.macro._seed_calendar`.

    FRED release IDs (verified via FRED catalog):
        10 = CPI, 50 = NFP, 54 = PCE, 53 = GDP, 46 = PPI, 34 = ECI
    """
    factory = client_factory or _build_default_client
    fred = factory()
    return _with_retry(
        fred.get_release_dates, release_id,
        start=start, end=end,
        include_release_dates_with_no_data=include_release_dates_with_no_data,
    )


def get_latest_value(
    series_id: str,
    *,
    client_factory: Callable[[], "Fred"] | None = None,
) -> float | None:
    """Convenience: get the most recent non-NaN value of ``series_id``.
    Returns None when the series is empty (covers brand-new series IDs
    or temporarily unavailable data)."""
    series = get_series(series_id, client_factory=client_factory)
    try:
        clean = series.dropna()
    except AttributeError:
        return None
    if clean.empty:
        return None
    return float(clean.iloc[-1])


__all__ = [
    "FredUnavailable",
    "get_series",
    "get_release_dates",
    "get_latest_value",
]
