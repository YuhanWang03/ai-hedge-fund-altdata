"""Non-overlapping, model-facing capability catalog for Agent V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    pack: str
    description: str
    input_schema: dict[str, Any]
    mutating: bool = False
    long_running: bool = False
    evidence_required: bool = True


class CapabilityCatalog:
    def __init__(self, specs: Iterable[CapabilitySpec] = ()) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name: str) -> CapabilitySpec | None:
        return self._specs.get(name)

    def names(self, packs: Iterable[str] | None = None) -> list[str]:
        allowed = set(packs or ())
        return [name for name, spec in self._specs.items() if not allowed or spec.pack in allowed]

    def specs(self, packs: Iterable[str] | None = None) -> list[CapabilitySpec]:
        return [self._specs[name] for name in self.names(packs)]

    def schemas(self, packs: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name.replace(".", "__"),
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
            for spec in self.specs(packs)
        ]


_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_TICKER = {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9.-]{0,7}$"}
_TICKERS = {"type": "array", "items": _TICKER}
_UNIVERSE = {
    "type": "string",
    "enum": [
        "custom",
        "tech30",
        "sp500",
        "nasdaq100",
        "dow30",
        "holdings",
        "watchlist",
        "holdings_watchlist",
    ],
}
_DATA_SOURCE = {"type": "string", "enum": ["yfinance", "fd"]}


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def default_catalog() -> CapabilityCatalog:
    specs = (
        CapabilitySpec("account.portfolio", "account", "Current positions and weights in the user's account.", _EMPTY),
        CapabilitySpec("account.performance", "account", "Account P&L for day, week, or month.", _object({"period": {"type": "string", "enum": ["day", "week", "month"]}}, ["period"])),
        CapabilitySpec("account.risk", "account", "Portfolio concentration, exposure, drawdown, and event risk.", _EMPTY),
        CapabilitySpec("account.earnings_schedule", "account", "Upcoming earnings across holdings and watchlist.", _object({"days": {"type": "integer", "minimum": 1, "maximum": 90}})),
        CapabilitySpec("research.stock", "research", "Evidence-backed research for one stock and one focus area.", _object({"ticker": _TICKER, "focus": {"type": "string", "enum": ["overview", "fundamentals", "valuation", "earnings", "market", "ownership", "catalysts", "filings", "supply_chain", "risk", "full"]}}, ["ticker"]), long_running=True),
        CapabilitySpec("research.compare", "research", "Compare two to four stocks on identical research dimensions.", _object({"tickers": {"type": "array", "items": _TICKER, "minItems": 2, "maxItems": 4}, "dimensions": {"type": "array", "items": {"type": "string"}}}, ["tickers"]), long_running=True),
        CapabilitySpec("research.changes", "research", "Compare the latest two stored research snapshots for one stock.", _object({"ticker": _TICKER}, ["ticker"])),
        CapabilitySpec("market.performance", "research", "Recent stock-price performance across day, week, month, quarter and year, including volume and sector/broad-market benchmarks. Use for recent performance, returns, price trend, or whether a stock is outperforming.", _object({"ticker": _TICKER}, ["ticker"])),
        CapabilitySpec("market.explain_move", "research", "Explain a recent price move and relative-market divergence.", _object({"ticker": _TICKER}, ["ticker"])),
        CapabilitySpec("institutional.manager_portfolio", "research", "Latest 13F portfolio for a named manager.", _object({"manager": {"type": "string"}}, ["manager"])),
        CapabilitySpec("etf.ark_activity", "research", "ARK ETF holdings and recent activity.", _object({"symbol": {"type": "string"}}, ["symbol"])),
        CapabilitySpec("macro.release", "research", "Latest value and date for a named macro release.", _object({"release_type": {"type": "string", "enum": ["cpi", "pce", "nfp", "gdp", "ppi", "claims", "fomc"]}}, ["release_type"])),
        CapabilitySpec(
            "lab.screen",
            "lab",
            "Run a deterministic stock screen over a selected universe.",
            _object(
                {
                    "universe": _UNIVERSE,
                    "tickers": _TICKERS,
                    "data_source": _DATA_SOURCE,
                    "with_earnings": {"type": "boolean"},
                    "rules": {
                        "type": "array",
                        "maxItems": 24,
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "op": {"type": "string", "enum": ["gte", "lte"]},
                                "value": {"type": "number"},
                            },
                            "required": ["field", "op", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "market_cap_min": {"type": "number", "minimum": 0},
                    "market_cap_max": {"type": "number", "exclusiveMinimum": 0},
                    "revenue_growth_min": {"type": "number"},
                    "gross_margin_min": {"type": "number"},
                    "volatility_max": {"type": "number", "exclusiveMinimum": 0},
                }
            ),
            long_running=True,
        ),
        CapabilitySpec(
            "lab.backtest",
            "lab",
            "Backtest an existing strategy with explicit assumptions and costs.",
            _object(
                {
                    "strategy": {"type": "string", "enum": ["pead", "momentum", "insider", "committee"]},
                    "universe": _UNIVERSE,
                    "tickers": _TICKERS,
                    "data_source": _DATA_SOURCE,
                    "holding_days": {"type": "integer", "minimum": 1, "maximum": 252},
                    "capital": {"type": "number", "exclusiveMinimum": 0},
                    "per_trade": {"type": "number", "exclusiveMinimum": 0},
                    "cost_bps": {"type": "number", "minimum": 0, "maximum": 200},
                    "earnings_limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "history_days": {"type": "integer", "minimum": 60, "maximum": 3650},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 60},
                    "lookback_days": {"type": "integer", "minimum": 20, "maximum": 504},
                    "skip_days": {"type": "integer", "minimum": 0, "maximum": 120},
                    "near_high_pct": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "window_days": {"type": "integer", "minimum": 1, "maximum": 180},
                    "min_insiders": {"type": "integer", "minimum": 1, "maximum": 20},
                    "min_value_usd": {"type": "number", "minimum": 0},
                    "min_consensus": {"type": "number", "minimum": -1, "maximum": 1},
                    "min_agreement": {"type": "number", "minimum": 0, "maximum": 1},
                    "personas": {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 20},
                    "lean": {"type": "boolean"},
                    "filing_lag_days": {"type": "integer", "minimum": 0, "maximum": 120},
                },
                ["strategy"],
            ),
            long_running=True,
        ),
        CapabilitySpec(
            "lab.sweep",
            "lab",
            "Run a bounded momentum parameter sweep.",
            _object(
                {
                    "universe": _UNIVERSE,
                    "tickers": _TICKERS,
                    "data_source": _DATA_SOURCE,
                    "history_days": {"type": "integer", "minimum": 60, "maximum": 3650},
                    "lookback_days": {"type": "integer", "minimum": 20, "maximum": 504},
                    "skip_days": {"type": "integer", "minimum": 0, "maximum": 120},
                    "capital": {"type": "number", "exclusiveMinimum": 0},
                    "cost_bps": {"type": "number", "minimum": 0, "maximum": 200},
                    "top_ns": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 6},
                    "holding_days_list": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 6},
                    "near_high_pcts": {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 1, "maxItems": 4},
                }
            ),
            long_running=True,
        ),
        CapabilitySpec(
            "lab.event_study",
            "lab",
            "Measure abnormal returns around earnings events.",
            _object(
                {
                    "universe": _UNIVERSE,
                    "tickers": _TICKERS,
                    "data_source": _DATA_SOURCE,
                    "earnings_limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "n_bootstrap": {"type": "integer", "minimum": 100, "maximum": 10000},
                    "require_eps_surprise": {"type": "boolean"},
                    "dedupe": {"type": "boolean"},
                    "group_by": {"type": "string", "enum": ["surprise", "reaction", "source"]},
                }
            ),
            long_running=True,
        ),
        CapabilitySpec(
            "lab.committee",
            "lab",
            "Run the deterministic investor-persona committee.",
            _object(
                {
                    "source": {"type": "string", "enum": ["tickers", "holdings", "watchlist", "screening"]},
                    "tickers": _TICKERS,
                    "personas": {"type": ["array", "null"], "items": {"type": "string"}},
                    "as_of": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "top_n": {"type": ["integer", "null"], "minimum": 1, "maximum": 60},
                    "use_cache": {"type": "boolean"},
                    "max_weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                    "lean": {"type": "boolean"},
                    "screening": {"type": ["object", "null"]},
                }
            ),
            long_running=True,
        ),
        CapabilitySpec("lab.result", "lab", "Read an existing asynchronous lab result.", _object({"run_id": {"type": "string"}}, ["run_id"])),
        CapabilitySpec("state.read", "account", "Read watchlist, alerts, or user settings.", _object({"section": {"type": "string", "enum": ["watchlist", "alerts", "settings"]}}, ["section"])),
        CapabilitySpec("state.mutate", "command", "Apply a previously confirmed watchlist or alert mutation.", _object({"operation": {"type": "string"}, "payload": {"type": "object"}}, ["operation", "payload"]), mutating=True),
        CapabilitySpec("web.research", "web", "Search bounded external evidence when internal sources have a documented gap.", _object({"query": {"type": "string", "minLength": 1, "maxLength": 500}, "topic": {"type": "string"}, "ticker": _TICKER, "recency_days": {"type": "integer", "minimum": 1, "maximum": 3650}}, ["query", "topic"]), long_running=True),
    )
    return CapabilityCatalog(specs)
