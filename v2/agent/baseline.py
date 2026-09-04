"""Baseline: the existing single-hop path, wrapped for measurement.

``v2/bot/commands.py:cmd_nl`` is the incumbent — classify once, dispatch once,
return the responder's card verbatim. It is also entangled with Telegram
(placeholder messages, HTML parse modes, async handlers), so it cannot be
measured as-is.

This module reproduces that control flow faithfully and nothing else:

    parsed = intent.classify(text)     # one LLM call
    result = TOOL[parsed.intent](args) # one tool call
    answer = result                    # verbatim; the LLM writes nothing

Two properties of the original are preserved on purpose, because they are what
the comparison is about:

* exactly one tool call per query — no follow-ups, whatever comes back;
* the model never authors prose, so the baseline cannot invent a figure. Its
  grounding score is 1.0 by construction. That is a real advantage, and the
  comparison is dishonest if it is hidden.

Routing both modes through the same :class:`ToolRegistry` keeps the measurement
apples-to-apples: same executor, same fixtures, same accounting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from v2.agent.registry import ToolRegistry, ToolResult


# intent name -> (tool name, argument builder over intent.classify's dict)
INTENT_TO_TOOL: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "explain_move":      ("explain_move",      lambda p: {"ticker": p.get("ticker", "")}),
    "summary":           ("summary",           lambda p: {"ticker": p.get("ticker", "")}),
    "chain":             ("chain",             lambda p: {"ticker": p.get("ticker", "")}),
    "thirteen_f":        ("institutional_13f", lambda p: {"manager": p.get("manager", "")}),
    "holders_view":      ("holders",           lambda p: {"ticker": p.get("ticker", "")}),
    "etf_view":          ("etf_view",          lambda p: {"symbol": p.get("etf", "") or "ARKK"}),
    "watchlist_view":    ("watchlist_view",    lambda p: {}),
    "watchlist_add":     ("watchlist_add",     lambda p: {"ticker": p.get("ticker", "")}),
    "watchlist_remove":  ("watchlist_remove",  lambda p: {"ticker": p.get("ticker", "")}),
    "settings":          ("settings_view",     lambda p: {}),
    "alert_set":         ("alert_set",         lambda p: {"ticker": p.get("ticker", ""),
                                                          "target_price": p.get("target_price", 0),
                                                          "direction": p.get("direction") or "above"}),
    "alert_list":        ("alert_list",        lambda p: {}),
    "portfolio_view":    ("portfolio_view",    lambda p: {}),
    "pnl_view":          ("pnl_view",          lambda p: {}),
    "earnings_view":     ("earnings_view",     lambda p: {"ticker": p.get("ticker", "")}),
    "earnings_calendar": ("earnings_calendar", lambda p: {"days_horizon": p.get("days_horizon") or 14}),
    "risk_view":         ("risk_view",         lambda p: {}),
    "pnl_period":        ("pnl_period",        lambda p: {"period": p.get("period") or "day"}),
    "eight_k_view":      ("eight_k_view",      lambda p: {"ticker": p.get("ticker", "")}),
    "insider_view":      ("insider_view",      lambda p: {"ticker": p.get("ticker", ""),
                                                          **({"days_back": p["days_back"]}
                                                             if p.get("days_back") else {})}),
    "macro_view":        ("macro_view",        lambda p: {}),
    "release_check":     ("release_check",     lambda p: {"release_type": p.get("release_type") or "cpi"}),
    "moneyflow_view":    ("moneyflow_view",    lambda p: {"ticker": p.get("ticker", "")}),
}

# Present in the bot's enum but backed by a private helper rather than a
# responder, so it has no tool in this harness. Recorded, not silently dropped.
UNROUTED_INTENTS = {"find_anomalies"}


@dataclass
class BaselineResult:
    """Same shape as AgentResult where it overlaps, so both can go in one table."""

    query: str
    answer: str
    intent: str
    tool: str
    elapsed_ms: int
    result: ToolResult | None = None
    parsed: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def stats(self) -> dict[str, Any]:
        return {
            "mode": "baseline",
            "llm_calls": 1,                       # classification only
            "tool_calls": 1 if self.result else 0,
            "failed_tool_calls": 0 if (self.result and self.result.ok) else (1 if self.result else 0),
            "distinct_tools": 1 if self.result else 0,
            "elapsed_ms": self.elapsed_ms,
            "stop_reason": "single_hop",
            # The model never writes prose here, so no figure can be invented.
            "grounding_ratio": 1.0,
            "ungrounded_figures": 0,
            "repairs": 0,
            "deduped_calls": 0,
            "intent": self.intent,
        }


class ScriptedClassifier:
    """Replays fixed ``intent.classify`` outputs so the baseline is testable offline."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        if not self.results:
            return {"intent": "unknown", "raw": text}
        return self.results.pop(0)


def _default_classifier(text: str) -> dict[str, Any]:
    """The real thing — imported lazily so this module stays dependency-free."""
    from v2.bot import intent
    return intent.classify(text)


def run_baseline(
    query: str,
    *,
    classifier: Callable[[str], dict[str, Any]] | None = None,
    registry: ToolRegistry | None = None,
) -> BaselineResult:
    """Answer ``query`` the way the current bot does: one label, one call."""
    classify = classifier or _default_classifier
    registry = registry or ToolRegistry()
    started = time.time()

    try:
        parsed = classify(query)
    except Exception as exc:  # noqa: BLE001 — mirrors intent.classify's own guard
        return BaselineResult(query=query, answer=f"分类失败：{type(exc).__name__}",
                              intent="unknown", tool="",
                              elapsed_ms=int((time.time() - started) * 1000),
                              error=str(exc))

    name = str(parsed.get("intent", "unknown"))

    if name in UNROUTED_INTENTS:
        return BaselineResult(
            query=query, intent=name, tool="",
            answer="（该 intent 由 bot 内部私有函数处理，未在对比工具层中暴露）",
            elapsed_ms=int((time.time() - started) * 1000), parsed=parsed,
        )

    route = INTENT_TO_TOOL.get(name)
    if route is None:
        return BaselineResult(
            query=query, intent="unknown", tool="",
            answer="❓ 没听懂。可以试试 /help 看看支持哪些命令。",
            elapsed_ms=int((time.time() - started) * 1000), parsed=parsed,
        )

    tool_name, build_args = route
    result = registry.call(tool_name, build_args(parsed))
    return BaselineResult(
        query=query,
        answer=result.as_observation(),
        intent=name,
        tool=tool_name,
        elapsed_ms=int((time.time() - started) * 1000),
        result=result,
        parsed=parsed,
    )
