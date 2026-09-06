"""One fundamentals snapshot per ticker, shared by all thirteen personas.

Upstream, every agent re-fetched its own metrics, line items and market cap,
so a full run cost 40-60 API calls per ticker.  Here the data is pulled once
(8 calls at most), frozen into a :class:`PersonaSnapshot`, and every persona
reads its slice.  The snapshot hashes its own content so a cache can tell
"same quarter, nothing changed" from "new filing, re-run".
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from v2.personas.data import ALL_LINE_ITEMS, PersonaDataClient, adapt_client
from v2.personas.models import Record, as_records

logger = logging.getLogger(__name__)

#: Which optional pieces a persona needs. ``metrics`` and ``line_items`` are
#: always fetched; the rest only when at least one selected persona asks.
OPTIONAL_NEEDS = ("insiders", "news", "prices")


@dataclass
class PersonaSnapshot:
    ticker: str
    as_of: str
    metrics_ttm: list[Record] = field(default_factory=list)
    metrics_annual: list[Record] = field(default_factory=list)
    line_items_ttm: list[Record] = field(default_factory=list)
    line_items_annual: list[Record] = field(default_factory=list)
    market_cap: float | None = None
    insider_trades: list[Record] = field(default_factory=list)
    news: list[Record] = field(default_factory=list)
    prices: list[Record] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    fetched_at: str = ""

    # -- accessors the personas use --------------------------------------------

    def metrics(self, period: str = "ttm", limit: int | None = None) -> list[Record]:
        rows = self.metrics_annual if period == "annual" else self.metrics_ttm
        return rows[:limit] if limit else list(rows)

    def line_items(self, period: str = "ttm", limit: int | None = None) -> list[Record]:
        rows = self.line_items_annual if period == "annual" else self.line_items_ttm
        return rows[:limit] if limit else list(rows)

    @property
    def has_fundamentals(self) -> bool:
        return bool(self.metrics_ttm or self.metrics_annual or self.line_items_ttm or self.line_items_annual)

    # -- identity ----------------------------------------------------------------

    @property
    def content_hash(self) -> str:
        """Stable digest of the data (not of when it was fetched)."""
        body = self.to_dict()
        body.pop("fetched_at", None)
        body.pop("gaps", None)
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of,
            "metrics_ttm": [r.to_dict() for r in self.metrics_ttm],
            "metrics_annual": [r.to_dict() for r in self.metrics_annual],
            "line_items_ttm": [r.to_dict() for r in self.line_items_ttm],
            "line_items_annual": [r.to_dict() for r in self.line_items_annual],
            "market_cap": self.market_cap,
            "insider_trades": [r.to_dict() for r in self.insider_trades],
            "news": [r.to_dict() for r in self.news],
            "prices": [r.to_dict() for r in self.prices],
            "gaps": list(self.gaps),
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaSnapshot":
        return cls(
            ticker=data["ticker"],
            as_of=data["as_of"],
            metrics_ttm=as_records(data.get("metrics_ttm")),
            metrics_annual=as_records(data.get("metrics_annual")),
            line_items_ttm=as_records(data.get("line_items_ttm")),
            line_items_annual=as_records(data.get("line_items_annual")),
            market_cap=data.get("market_cap"),
            insider_trades=as_records(data.get("insider_trades")),
            news=as_records(data.get("news")),
            prices=as_records(data.get("prices")),
            gaps=list(data.get("gaps") or []),
            fetched_at=data.get("fetched_at", ""),
        )


def _as_of(value: str | date | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _sorted_prices(rows: Iterable[Record]) -> list[Record]:
    return sorted(rows, key=lambda p: str(p.time or p.date or ""))


def build_snapshot(
    ticker: str,
    as_of: str | date | None = None,
    client: PersonaDataClient | Any | None = None,
    *,
    need: Iterable[str] = OPTIONAL_NEEDS,
    limit: int = 10,
    lookback_days: int = 365,
    line_items: Iterable[str] = ALL_LINE_ITEMS,
) -> PersonaSnapshot:
    """Fetch everything the selected personas need for ``ticker`` as of a date.

    Every fetch is independent: a failing endpoint records a gap and the
    personas that depend on it abstain, the rest still run.
    """
    ticker = ticker.strip().upper()
    end = _as_of(as_of)
    start = (date.fromisoformat(end) - timedelta(days=lookback_days)).isoformat()
    need = set(need)
    fd = adapt_client(client)
    snap = PersonaSnapshot(ticker=ticker, as_of=end, fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def attempt(label: str, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotImplementedError as exc:
            snap.gaps.append(f"{label}: {exc}")
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the run
            logger.warning("%s %s failed: %s", ticker, label, exc)
            snap.gaps.append(f"{label}: {type(exc).__name__}: {str(exc)[:120]}")
        return None

    wanted_items = list(line_items)
    snap.metrics_ttm = as_records(attempt("metrics_ttm", fd.get_financial_metrics, ticker, end, period="ttm", limit=limit) or [])
    snap.metrics_annual = as_records(attempt("metrics_annual", fd.get_financial_metrics, ticker, end, period="annual", limit=limit) or [])
    snap.line_items_ttm = as_records(attempt("line_items_ttm", fd.search_line_items, ticker, wanted_items, end, period="ttm", limit=limit) or [])
    snap.line_items_annual = as_records(attempt("line_items_annual", fd.search_line_items, ticker, wanted_items, end, period="annual", limit=limit) or [])
    snap.market_cap = attempt("market_cap", fd.get_market_cap, ticker, end)
    if snap.market_cap is None:
        for rows in (snap.metrics_ttm, snap.metrics_annual):
            if rows and rows[0].market_cap:
                snap.market_cap = float(rows[0].market_cap)
                break
    if "insiders" in need:
        snap.insider_trades = as_records(attempt("insider_trades", fd.get_insider_trades, ticker, end, start_date=start, limit=1000) or [])
    if "news" in need:
        snap.news = as_records(attempt("news", fd.get_company_news, ticker, end, start_date=start, limit=250) or [])
    if "prices" in need:
        snap.prices = _sorted_prices(as_records(attempt("prices", fd.get_prices, ticker, start, end) or []))
    if not snap.has_fundamentals:
        snap.gaps.append("fundamentals: no metrics or line items returned")
    return snap
