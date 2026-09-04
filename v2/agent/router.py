"""Routing layer — decide whether a query takes the fast path or the agent loop.

The economics, measured on this project's own tools (see the CLI's comparison
table): the single-hop path costs 1 LLM call and ~1.2s; the agent loop costs 4-5
calls, ~11s and ~14k tokens. That is a 10x cost multiplier which pays for itself
only on questions the fast path cannot answer at all. So the router's job is to
spend it rarely and deliberately.

**The router must not call an LLM.** A classifier call to decide whether to make
more classifier calls would give back the savings it exists to protect. Every
signal below is a regex over the raw text plus the intent the bot's existing
classifier already produced — zero marginal cost.

These rules are heuristics and some will misfire. They are written as a table of
named predicates rather than an if-chain so that each one can be measured
separately against a labelled set and tuned on evidence (``samples.py`` is the
seed of that set). A decision records which signal fired, so a wrong route is
diagnosable rather than mysterious.

Modes, which are also the rollout order:

    off           everything takes the fast path — current production behaviour
    unknown_only  only queries the classifier rejects reach the agent
    heuristic     the full signal table below
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Literal

Path = Literal["slash", "single_hop", "agent"]

ROUTING_MODES = ("off", "unknown_only", "heuristic")
DEFAULT_MODE = "off"

#: Explicit user escape hatch — force the agent regardless of mode.
ASK_PREFIX = "/ask"


@dataclass(frozen=True)
class RouteDecision:
    """Where a query goes, and why. The 'why' is what makes routing tunable."""

    path: Path
    signal: str      # which rule fired; "" when nothing did
    reason: str      # human-readable, shown in the routing chip and logs
    mode: str

    @property
    def is_agent(self) -> bool:
        return self.path == "agent"


def routing_mode() -> str:
    """Read the flag. Anything unrecognised degrades to 'off' — a typo in an env
    var must never silently enable a 10x-cost path in production."""
    mode = os.environ.get("V2_AGENT_ROUTING", DEFAULT_MODE).strip().lower()
    return mode if mode in ROUTING_MODES else DEFAULT_MODE


# ---------------------------------------------------------------------------
# Signal predicates
# ---------------------------------------------------------------------------

# 最近 / 最新 / 最后 / 最终 are time words, not superlatives — excluding them is
# the single most important detail here, since "NVDA 最近怎么样" is a plain
# single-ticker lookup and must stay on the fast path.
_SUPERLATIVE = re.compile(
    r"最(?!近|新|后|终)|哪[只个几支家]|排[序名]|对比|比较|谁更|更(?:好|差|危险|安全|强|严重)"
    r"|\bvs\b|versus|排行"
)

#: Superlatives that ask for a *composite judgment* rather than a ranking on one
#: printed column. "哪只跌得最多" is answered by the positions card itself;
#: "哪只最危险" is not, because danger is assembled from weight, earnings
#: proximity and insider activity, which live in three different tools.
_COMPOSITE_JUDGMENT = re.compile(
    r"危险|值得关注|该关注|该注意|最该|严重|健康|靠谱|安全|踩雷|暴雷|有问题")

# References to a *set* the user owns, rather than one named security.
_COLLECTION = re.compile(
    r"持仓[里中]|我的持仓|仓位[里中]|组合[里中]|关注(?:列表|的)|watchlist"
    r"|我的股票|哪些(?:持仓|股票|票)|全部持仓|所有持仓"
)

#: Asking how the members of a set are *doing* needs a lookup per member, even
#: when one card can list the members.
_PER_ITEM_STATE = re.compile(r"怎么样|表现|情况|如何|怎样")

# Several asks welded into one message.
_COMPOUND = re.compile(r"并且|还有|另外|顺便|同时|以及|,\s*还|，还|;|；")

# Causal questions that span the book rather than one ticker.
_CAUSAL = re.compile(r"为什么|为啥|怎么回事|原因是|什么导致|怎么来的|谁拖|拖累")
_PNL_WORD = re.compile(r"亏|赚|盈|亏损|收益|回撤|drawdown")

#: Two different periods in one question means two lookups, always.
_PERIOD_WORDS = (
    re.compile(r"上周|前一周"), re.compile(r"这周|本周|这个星期"),
    re.compile(r"上个?月|前一月"), re.compile(r"这个?月|本月"),
    re.compile(r"昨天|昨日"), re.compile(r"今天|当日|今日"),
)

#: Subject areas, each backed by a *different* tool. Deliberately excludes
#: generic container words (持仓 / 组合 / 仓位): they appear in almost every
#: portfolio question and would make everything look multi-topic.
_TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("earnings", re.compile(r"财报|业绩|EPS|surprise", re.I)),
    ("sec", re.compile(r"8-?K|申报|公告|SEC", re.I)),
    ("insider", re.compile(r"内部人|高管|form ?4|减持|增持", re.I)),
    ("flow", re.compile(r"资金流|CMF|RSI|吸筹|流入|流出", re.I)),
    ("move", re.compile(r"为什么|为啥|原因|怎么回事|异动|什么导致|出什么事|怎么了")),
    ("risk", re.compile(r"风险|回撤|集中度")),
    ("pnl", re.compile(r"盈亏|收益率|赚了|亏了|回本|补回|p&l|pnl", re.I)),
    ("macro", re.compile(r"宏观|VIX|CPI|PCE|NFP|FOMC|利率|收益率曲线", re.I)),
    ("institutional", re.compile(r"机构|13F|巴菲特|burry|大佬", re.I)),
    ("etf", re.compile(r"ETF|ARK", re.I)),
    ("chain", re.compile(r"产业链|上下游|同业")),
    ("watchlist", re.compile(r"关注列表|watchlist", re.I)),
)

#: Cards that already aggregate across the whole book. When the classifier lands
#: on one of these, a question about that card's own subject is answered in one
#: call — escalating it buys nothing.
_INTENT_OWN_TOPIC: dict[str, str] = {
    "portfolio_view": "portfolio",
    "pnl_view": "pnl",
    "pnl_period": "pnl",
    "risk_view": "risk",
    "earnings_calendar": "earnings",
    "watchlist_view": "watchlist",
    "macro_view": "macro",
    "release_check": "macro",
    "alert_list": "alerts",
    "settings": "settings",
}
AGGREGATE_INTENTS = frozenset(_INTENT_OWN_TOPIC)

# Rough ticker detector: 2-5 uppercase letters, minus common English words that
# appear in bilingual queries. Deliberately loose — it only feeds a signal, and
# a false positive costs one unnecessary agent run, not a wrong answer.
_TICKER_LIKE = re.compile(r"\b[A-Z]{2,5}\b")
_NOT_TICKERS = frozenset({
    "AI", "US", "USD", "CEO", "CFO", "COO", "CTO", "SEC", "ETF", "IPO", "EPS",
    "PE", "PB", "ROE", "GDP", "CPI", "PCE", "NFP", "PPI", "FOMC", "FED", "RSI",
    "CMF", "OK", "VS", "AND", "THE", "FOR", "NL", "LLM", "API", "MD", "ARK",
})


def _tickers(text: str) -> list[str]:
    return [t for t in _TICKER_LIKE.findall(text or "") if t not in _NOT_TICKERS]


def topics(text: str) -> set[str]:
    return {name for name, pattern in _TOPICS if pattern.search(text or "")}


def _beyond_own_card(text: str, parsed: dict) -> set[str]:
    """Topics the query raises that the classifier's own card does not cover.

    This is the distinction the first version of this table missed. It looked
    only at how a question was *worded*, so "我持仓里哪只跌得最多" and
    "我持仓里哪只最危险" were indistinguishable — yet the first is a column of
    the positions card and the second needs three more tools.
    """
    own = _INTENT_OWN_TOPIC.get(str(parsed.get("intent", "")), "")
    return {t for t in topics(text) if t != own}


def _sig_multi_topic(text: str, parsed: dict) -> bool:
    """Two or more subject areas, each served by a different tool."""
    return len(topics(text)) >= 2 and len(_beyond_own_card(text, parsed)) >= 1


def _sig_superlative(text: str, parsed: dict) -> bool:
    """Ranking or comparison that the classifier's own card cannot settle."""
    if not _SUPERLATIVE.search(text):
        return False
    if _COMPOSITE_JUDGMENT.search(text):
        return True          # composite judgments always need several tools
    if str(parsed.get("intent", "")) in AGGREGATE_INTENTS:
        # Ranking on a column the aggregate card already prints.
        return bool(_beyond_own_card(text, parsed))
    return True


def _sig_collection(text: str, parsed: dict) -> bool:
    """Asks about a set the user owns, with no single ticker to look up."""
    if not _COLLECTION.search(text):
        return False
    if parsed.get("ticker"):
        return False
    if str(parsed.get("intent", "")) in AGGREGATE_INTENTS:
        # The card lists the set. It still needs escalating when the question
        # asks for something outside that card, or how each member is doing.
        return (bool(_beyond_own_card(text, parsed))
                or bool(_PER_ITEM_STATE.search(text))
                or bool(_COMPOSITE_JUDGMENT.search(text)))
    return True


def _sig_period_comparison(text: str, parsed: dict) -> bool:
    """Two distinct periods in one question — one card covers one period."""
    return sum(1 for pattern in _PERIOD_WORDS if pattern.search(text)) >= 2


def _sig_compound(text: str, parsed: dict) -> bool:
    """Two questions in one message — one tool call can only answer one."""
    if _COMPOUND.search(text):
        return True
    return (text.count("?") + text.count("？")) >= 2


def _sig_causal_portfolio(text: str, parsed: dict) -> bool:
    """'Why did I lose money' — causal, about the book, not one ticker."""
    if not _CAUSAL.search(text) or parsed.get("ticker"):
        return False
    if str(parsed.get("intent", "")) in AGGREGATE_INTENTS:
        # e.g. pnl_period already prints per-position contributions.
        return False
    return bool(_PNL_WORD.search(text) or _COLLECTION.search(text))


def _sig_multi_ticker(text: str, parsed: dict) -> bool:
    """Two or more named securities — necessarily more than one lookup."""
    return len(set(_tickers(text))) >= 2


#: Evaluated in order; the first match wins and is recorded on the decision.
HEURISTIC_SIGNALS: tuple[tuple[str, Callable[[str, dict], bool], str], ...] = (
    ("multi_topic", _sig_multi_topic, "涉及多个由不同工具支撑的主题"),
    ("period_comparison", _sig_period_comparison, "要对比两个时间段，一张卡只覆盖一个"),
    ("superlative", _sig_superlative, "要求的排序/比较超出单张卡能给的范围"),
    ("collection", _sig_collection, "问的是持仓/关注列表整体，需要先枚举再逐个查"),
    ("multi_ticker", _sig_multi_ticker, "同时问了多只股票"),
    ("causal_portfolio", _sig_causal_portfolio, "组合层面的因果问题，需要归因链"),
    ("compound", _sig_compound, "一句话里有多个独立问题"),
)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route(text: str, parsed: dict | None = None, *, mode: str | None = None) -> RouteDecision:
    """Decide the path for one query.

    Args:
        text: the user's raw message.
        parsed: the output of the bot's intent classifier. Passed in rather than
            computed here so the caller never pays for a second classification.
        mode: override the env flag (tests, evaluation sweeps).
    """
    mode = mode or routing_mode()
    parsed = parsed or {}
    stripped = (text or "").strip()

    # Explicit escape always wins — a user who typed /ask meant it.
    if stripped.lower().startswith(ASK_PREFIX):
        return RouteDecision("agent", "explicit_ask", "用户用 /ask 显式要求 agent", mode)

    if stripped.startswith("/"):
        return RouteDecision("slash", "slash_command", "斜杠命令已指定工具，无需规划", mode)

    if mode == "off":
        return RouteDecision("single_hop", "", "路由未启用（V2_AGENT_ROUTING=off）", mode)

    # The classifier's dead end is the safest first thing to hand over: these
    # queries produce "❓ 没听懂" today, so any answer is an improvement and
    # nothing that currently works can regress.
    if parsed.get("intent", "unknown") == "unknown":
        return RouteDecision("agent", "unknown_intent", "单跳分类器无法归类，交给 agent", mode)

    if mode == "unknown_only":
        return RouteDecision("single_hop", "", "已命中 intent，按现有单跳路径处理", mode)

    for name, predicate, reason in HEURISTIC_SIGNALS:
        try:
            if predicate(stripped, parsed):
                return RouteDecision("agent", name, reason, mode)
        except Exception:  # noqa: BLE001 — a broken predicate must not break routing
            continue

    return RouteDecision("single_hop", "", "单个工具即可回答", mode)


def explain(decision: RouteDecision) -> str:
    """One-line chip for the user, mirroring the bot's existing 🎯 routing chip."""
    if decision.path == "agent":
        return f"🧭 多步分析（{decision.reason}）"
    if decision.path == "slash":
        return "⚡ 直接执行"
    return "⚡ 单跳查询"
