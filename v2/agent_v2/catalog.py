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
    #: Domain-specific instructions the synthesizer appends when this
    #: capability contributed evidence.  Keeps domain prose out of the core.
    answer_guidance: str = ""


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


_RESEARCH_GUIDANCE = "stock_research：围绕公司的核心投资矛盾组织答案，不逐项报分。ROIC、ROE、利润率等异常高于 100% 的比率必须提示其依赖数据与计算口径，不能当作无条件质量结论。"


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
        CapabilitySpec(
            "research.stock",
            "research",
            "Evidence-backed research for one stock and one focus area.",
            _object({"ticker": _TICKER, "focus": {"type": "string", "enum": ["overview", "fundamentals", "valuation", "earnings", "market", "ownership", "catalysts", "filings", "supply_chain", "risk", "full"]}}, ["ticker"]),
            long_running=True,
            answer_guidance=_RESEARCH_GUIDANCE,
        ),
        CapabilitySpec(
            "research.compare",
            "research",
            "Compare two to four stocks on identical research dimensions.",
            _object({"tickers": {"type": "array", "items": _TICKER, "minItems": 2, "maxItems": 4}, "dimensions": {"type": "array", "items": {"type": "string"}}}, ["tickers"]),
            long_running=True,
            answer_guidance=_RESEARCH_GUIDANCE,
        ),
        CapabilitySpec("research.changes", "research", "Compare the latest two stored research snapshots for one stock.", _object({"ticker": _TICKER}, ["ticker"])),
        CapabilitySpec(
            "market.performance",
            "research",
            "Recent stock-price performance across day, week, month, quarter and year, including volume and sector/broad-market benchmarks. Use for recent performance, returns, price trend, or whether a stock is outperforming.",
            _object({"ticker": _TICKER}, ["ticker"]),
            answer_guidance=(
                "recent_performance：先回答最新交易日、近 5 日和近 1 月的价格回报，再说明相对行业或大盘基准的强弱及成交量；不得用营收、毛利率或估值代替价格表现。"
                "若 metrics.is_intraday=true，必须写“截至查询时”或“盘中”，不能写“收盘”；当前累计成交量只能与完整日均量做进度参考，不得据此判断放量、缩量或上涨持续性。数据不足时明确缺少哪个时间窗口。"
                "每一句含数字的行情事实都必须紧跟对应的 [evidence_id]。"
            ),
        ),
        CapabilitySpec(
            "market.explain_move",
            "research",
            "Explain a recent price move and relative-market divergence.",
            _object({"ticker": _TICKER}, ["ticker"]),
            answer_guidance=(
                "move_explanation：第一句回答是否上涨/下跌、日期、幅度和成交量。把已确认行情事实、高置信度直接驱动、普通候选解释分开。"
                "只有 metadata.claim_role=confirmed_driver 的证据才能写成已确认原因；candidate_driver 必须写成“可能相关”并说明中/低置信度。"
                "如果 confirmed_driver_count 为 0，用自然语言说“暂未找到可核实的同日催化剂，具体触发原因尚未确认”，不要输出“0 个驱动”之类的系统字段。"
                "不能把历史涨幅、机构持仓或时间不匹配的新闻写成当日直接原因。没有直接驱动时最多展示 1 条最相关的候选线索；metadata.citable=false 的线索不要引用。"
                "必须给出行业基准对比；若工具没有基准则说明缺失。若 metrics.is_intraday=true，必须标明盘中口径，且不得用当前累计成交量推断放量/缩量或持续性。"
                "每一句含数字的行情事实都必须紧跟对应的 [evidence_id]。"
            ),
        ),
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
        CapabilitySpec("state.read", "account", "Read watchlist, alerts, or user settings.", _object({"section": {"type": "string", "enum": ["watchlist", "alerts", "settings"]}}, ["section"])),
        CapabilitySpec(
            "state.mutate",
            "command",
            "Apply a previously confirmed watchlist or alert mutation.",
            _object(
                {
                    "operation": {"type": "string", "enum": ["watchlist.add", "watchlist.remove", "alert.add", "alert.remove"]},
                    "payload": {"type": "object"},
                },
                ["operation", "payload"],
            ),
            mutating=True,
        ),
        CapabilitySpec("web.research", "web", "Search bounded external evidence when internal sources have a documented gap.", _object({"query": {"type": "string", "minLength": 1, "maxLength": 500}, "topic": {"type": "string"}, "ticker": _TICKER, "recency_days": {"type": "integer", "minimum": 1, "maximum": 3650}}, ["query", "topic"]), long_running=True),
    )
    return CapabilityCatalog(specs)
