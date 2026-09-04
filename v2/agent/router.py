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
    r"最(?!近|新|后|终)|哪[只个几支家]|排[序名]|对比|比较|谁更|更(?:好|差|危险|安全)"
    r"|\bvs\b|versus|排行|最.*?的是"
)

# References to a *set* the user owns, rather than one named security.
_COLLECTION = re.compile(
    r"持仓[里中]|我的持仓|仓位[里中]|组合[里中]|关注(?:列表|的)|watchlist"
    r"|我的股票|哪些(?:持仓|股票|票)|全部持仓|所有持仓"
)

# Several asks welded into one message.
_COMPOUND = re.compile(r"并且|还有|另外|顺便|同时|以及|,\s*还|，还|;|；")

# Causal questions that span the book rather than one ticker.
_CAUSAL = re.compile(r"为什么|为啥|怎么回事|原因是|什么导致")
_PNL_WORD = re.compile(r"亏|赚|盈|亏损|收益|回撤|drawdown")

# Rough ticker detector: 2-5 uppercase letters, minus common English words that
# appear in bilingual queries. Deliberately loose — it only feeds a signal, and
# a false positive costs one unnecessary agent run, not a wrong answer.
_TICKER_LIKE = re.compile(r"\b[A-Z]{2,5}\b")
_NOT_TICKERS = frozenset({
    "AI", "US", "USD", "CEO", "CFO", "COO", "CTO", "SEC", "ETF", "IPO", "EPS",
    "PE", "PB", "ROE", "GDP", "CPI", "PCE", "NFP", "PPI", "FOMC", "FED", "RSI",
    "CMF", "OK", "VS", "AND", "THE", "FOR", "NL", "LLM", "API", "MD",
})


def _tickers(text: str) -> list[str]:
    return [t for t in _TICKER_LIKE.findall(text or "") if t not in _NOT_TICKERS]


def _sig_superlative(text: str, parsed: dict) -> bool:
    """Ranking or comparison — the answer is a verdict over several subjects."""
    return bool(_SUPERLATIVE.search(text))


#: Intents whose tool *is* the enumeration. "我关注了哪些股票" mentions a set,
#: but watchlist_view returns exactly that set in one call — routing it to the
#: agent would spend 10x for the identical card.
_ENUMERATOR_INTENTS = frozenset({
    "watchlist_view", "portfolio_view", "alert_list", "settings", "pnl_view",
})


def _sig_collection(text: str, parsed: dict) -> bool:
    """Asks about a set the user owns, with no single ticker to look up.

    'CRWD 在我持仓里表现如何' names its subject and stays on the fast path;
    '我持仓里哪些要发财报' does not, and needs the book enumerated first.

    The enumerator exemption is the subtle case: when the classifier already
    landed on the tool that returns the whole set, one call answers it — unless
    the user also asked to rank or compare, which the ordering above handles by
    firing the superlative signal first.
    """
    if not _COLLECTION.search(text):
        return False
    if parsed.get("ticker"):
        return False
    if parsed.get("intent") in _ENUMERATOR_INTENTS and not _SUPERLATIVE.search(text):
        return False
    return True


def _sig_compound(text: str, parsed: dict) -> bool:
    """Two questions in one message — one tool call can only answer one."""
    if _COMPOUND.search(text):
        return True
    return (text.count("?") + text.count("？")) >= 2


def _sig_causal_portfolio(text: str, parsed: dict) -> bool:
    """'Why did I lose money' — causal, and about the book, not a ticker."""
    if not _CAUSAL.search(text):
        return False
    if parsed.get("ticker"):
        return False
    return bool(_PNL_WORD.search(text) or _COLLECTION.search(text))


def _sig_multi_ticker(text: str, parsed: dict) -> bool:
    """Two or more named securities — necessarily more than one lookup."""
    return len(set(_tickers(text))) >= 2


#: Evaluated in order; the first match wins and is recorded on the decision.
#: Ordered by how confident the signal is, most confident first.
HEURISTIC_SIGNALS: tuple[tuple[str, Callable[[str, dict], bool], str], ...] = (
    ("superlative", _sig_superlative, "要求排序或比较，答案需要横跨多个标的"),
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
