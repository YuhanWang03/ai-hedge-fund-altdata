"""B1 — evidence top-up for anomalies the attribution pipeline could not explain.

Today ``v2/monitoring/attributor.py`` ends at the Verifier: if every reason
scores 低, or the entity filter rejected every news item, the card ships as-is
and the reader gets "something moved, we don't know why". The news search had
one shot and the pipeline has no way to ask a different question.

This module gives it exactly one more move, under constraints that come from the
job being **unattended**. A user-facing agent can be slow, chatty and
occasionally wrong because the user is right there to retry. A 17:35 cron cannot:

* **It runs only on the failure branch.** Anomalies that already have a 高/中
  reason never reach this code, so the normal path costs nothing.
* **Read-only tools, four of them.** 8-K, Form 4, earnings and money flow are
  where an unexplained move's cause usually lives when news search comes up
  empty. No portfolio tools, nothing that writes.
* **A per-run cap.** A day when 20 tickers trip the detector must not turn one
  cron into twenty agent runs; the top few by move size are topped up and the
  rest ship unchanged.
* **A hard deadline on a daemon thread.** A hung HTTP call inside a responder
  must not be able to hold the cron open — the worker is abandoned, not awaited.
* **Structured output, not prose.** The user-facing loop writes paragraphs; here
  the loop must return reasons in the same ``ScoredReason`` shape the card
  already renders, validated against the same 高/中/低 whitelist. Anything that
  fails to parse is discarded.
* **Grounding, then discard.** Every figure must trace to a tool result. A
  failed check drops the top-up rather than repairing it — no one is watching,
  so silence beats a confident guess.

Any failure at all returns ``None`` and the caller ships the existing card. The
deterministic path is never *replaced*, only appended to.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from v2.agent import grounding
from v2.agent.llm import LLMClient
from v2.agent.loop import AgentConfig, run_agent
from v2.agent.registry import TOOL_SPECS, ToolRegistry

#: The four read-only tools an unexplained move is usually hiding in.
ASSIST_TOOLS = ("eight_k_view", "insider_view", "earnings_view", "moneyflow_view")

VALID_CONFIDENCE = frozenset({"高", "中", "低"})


class AnomalyLike(Protocol):
    """Structural type — avoids importing pydantic models into this package."""

    ticker: str
    price_change_pct: float
    volume_ratio: float
    flags: list[str]
    reasons: list[Any]


@dataclass(frozen=True)
class AssistConfig:
    max_items_per_run: int = 3
    max_steps: int = 3
    max_tool_calls: int = 4
    deadline_seconds: float = 25.0
    #: Extra wall-clock allowed beyond the loop's own budget before the worker is
    #: abandoned. The loop checks its deadline between steps, so a single hung
    #: HTTP call can overshoot; this is the outer guarantee that the cron ends.
    hard_deadline_grace: float = 5.0
    require_grounding: bool = True


@dataclass
class AssistReason:
    """One reason the top-up produced, before it becomes a ScoredReason."""

    text: str
    confidence: str = "中"
    evidence_tool: str = ""

    def note(self) -> str:
        return f"agent 补齐 · {self.evidence_tool}" if self.evidence_tool else "agent 补齐"


@dataclass
class AssistOutcome:
    """Result of one top-up. Only applied to the anomaly when ``ok``."""

    ticker: str
    reasons: list[AssistReason] = field(default_factory=list)
    tools_used: tuple[str, ...] = ()
    tool_calls: int = 0
    tokens: int = 0
    elapsed_ms: int = 0
    grounding_ratio: float = 1.0
    outcome: str = "ok"          # ok | no_finding | ungrounded | unparsable | timeout | error
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok" and bool(self.reasons)


def enabled() -> bool:
    """Off unless explicitly switched on. Merging this file changes no behaviour."""
    return os.environ.get("V2_AGENT_ANOMALY_ASSIST", "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Which anomalies qualify
# ---------------------------------------------------------------------------

def needs_assist(anomaly: AnomalyLike) -> bool:
    """True when attribution produced nothing usable.

    Two shapes of failure, both meaning "the news search did not explain this":
    no reasons at all (entity filter rejected everything, or the Generator
    returned none), or reasons that the Verifier scored 低 across the board.
    """
    reasons = list(getattr(anomaly, "reasons", None) or [])
    if not reasons:
        return True
    return all(getattr(r, "confidence", "中") == "低" for r in reasons)


def candidate_score(anomaly: AnomalyLike) -> float:
    """Pre-attribution ranking proxy.

    The cron computes ``compute_importance`` *after* attribution, so it is not
    available when deciding who gets a top-up. These four fields are known at
    detection time and correlate with the same thing: how much a reader would
    want this one explained.
    """
    score = abs(getattr(anomaly, "price_change_pct", 0.0) or 0.0) * 100.0
    score += min(getattr(anomaly, "volume_ratio", 0.0) or 0.0, 10.0)
    score += 2.0 * len(getattr(anomaly, "flags", None) or [])
    if getattr(anomaly, "contrarian", False):
        score += 3.0        # moved against its sector — the most interesting case
    return score


def select_candidates(anomalies: list[AnomalyLike], config: AssistConfig | None = None) -> list[AnomalyLike]:
    """The unexplained anomalies worth spending an agent run on, capped."""
    config = config or AssistConfig()
    eligible = [a for a in anomalies if needs_assist(a)]
    eligible.sort(key=candidate_score, reverse=True)
    return eligible[: config.max_items_per_run]


# ---------------------------------------------------------------------------
# Prompt + parsing
# ---------------------------------------------------------------------------

_ASSIST_PROMPT = """你在为一次**无法解释的股票异动**补齐证据。

新闻搜索已经失败了——要么没搜到相关报道，要么搜到的都被判定为低置信。
你的任务是换一个方向找原因：查公司自己的申报和交易数据。

# 可用工具（只读，共 4 个）
- eight_k_view: SEC 8-K 重大事项（高管变动、并购、业绩预告）
- insider_view: SEC Form 4 内部人买卖
- earnings_view: 财报日期与上次的 beat/miss
- moneyflow_view: 资金流向与超买超卖

# 规则
1. 先想这个异动最可能的原因类型，再决定查哪几个工具。可以一轮并行调用。
2. 只在**确实找到能解释异动的证据**时才给出理由。**找不到就明确说找不到**——
   一条编造的理由比一张"原因未知"的卡片有害得多。
3. 每条理由里的数字必须来自工具返回。
4. 置信度按证据强度给：
   - 高：公司层面的直接事件，且量级能匹配异动幅度
   - 中：相关但间接，或量级不完全匹配
   - 低：只是背景信息，不足以解释这次异动（这种就别给了）

# 输出格式
最后一轮只输出 JSON，不要任何其他文字：
{"reasons": [{"text": "理由原文", "confidence": "高|中|低", "evidence_tool": "工具名"}]}
找不到原因时输出：{"reasons": []}
"""


def _strip_fence(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        lines = body.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    return body


def _parse_reasons(answer: str) -> tuple[list[AssistReason], str]:
    """Parse the structured final answer. Anything malformed is discarded."""
    body = _strip_fence(answer)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return [], "no JSON object in final answer"
    try:
        parsed = json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return [], "top level is not an object"

    out: list[AssistReason] = []
    for item in parsed.get("reasons") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        confidence = str(item.get("confidence", "中")).strip()
        if confidence not in VALID_CONFIDENCE:
            confidence = "中"        # same posture as the bot's enum coercion
        tool = str(item.get("evidence_tool", "")).strip()
        if tool and tool not in ASSIST_TOOLS:
            tool = ""
        out.append(AssistReason(text=text, confidence=confidence, evidence_tool=tool))
    return out, ""


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------

def run_with_deadline(fn: Callable[[], Any], seconds: float) -> tuple[Any, bool]:
    """Run ``fn`` on a daemon thread, giving up after ``seconds``.

    A daemon thread, not an executor: if a responder's HTTP call hangs past the
    deadline, the interpreter must still be able to exit when the cron script
    finishes. An abandoned worker in a short-lived process is the cheap price of
    guaranteeing the cron always terminates.
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — reported, never raised here
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True, name="anomaly-assist")
    worker.start()
    worker.join(timeout=seconds)
    if worker.is_alive():
        return None, True
    if "error" in box:
        raise box["error"]
    return box.get("value"), False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_query(anomaly: AnomalyLike) -> str:
    flags = ", ".join(getattr(anomaly, "flags", None) or []) or "无"
    return (
        f"{anomaly.ticker} 今日异动：涨跌 {anomaly.price_change_pct:+.2%}，"
        f"成交量 {anomaly.volume_ratio:.1f}× 30 日均量，触发信号：{flags}。\n"
        f"新闻搜索没能给出可信解释，请从 SEC 申报和交易数据里找原因。"
    )


def assist(
    anomaly: AnomalyLike,
    *,
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    config: AssistConfig | None = None,
) -> AssistOutcome:
    """One evidence top-up. Never raises; ``outcome.ok`` says whether to use it."""
    config = config or AssistConfig()
    started = time.time()
    result = AssistOutcome(ticker=getattr(anomaly, "ticker", "?"))

    if registry is None:
        specs = tuple(s for s in TOOL_SPECS if s.name in ASSIST_TOOLS)
        registry = ToolRegistry(specs=specs, allow_mutations=False)

    agent_config = AgentConfig(
        max_steps=config.max_steps,
        max_tool_calls=config.max_tool_calls,
        max_seconds=config.deadline_seconds,
        allow_mutations=False,
        grounding_repair=False,      # unattended: discard, don't negotiate
        system_prompt=_ASSIST_PROMPT,
    )

    try:
        agent_result, timed_out = run_with_deadline(
            lambda: run_agent(_build_query(anomaly), llm=llm,
                              registry=registry, config=agent_config),
            config.deadline_seconds + config.hard_deadline_grace,
        )
    except Exception as exc:  # noqa: BLE001 — the cron must survive anything
        result.outcome = "error"
        result.detail = f"{type(exc).__name__}: {exc}"
        result.elapsed_ms = int((time.time() - started) * 1000)
        return result

    result.elapsed_ms = int((time.time() - started) * 1000)

    if timed_out or agent_result is None:
        result.outcome = "timeout"
        result.detail = (f"exceeded {config.deadline_seconds + config.hard_deadline_grace}s"
                         " — worker abandoned so the cron can exit")
        return result

    result.tools_used = tuple(agent_result.trajectory.distinct_tools())
    result.tool_calls = agent_result.trajectory.tool_calls
    result.tokens = agent_result.trajectory.prompt_tokens + agent_result.trajectory.completion_tokens

    reasons, parse_error = _parse_reasons(agent_result.answer)
    if parse_error:
        result.outcome = "unparsable"
        result.detail = parse_error
        return result
    if not reasons:
        result.outcome = "no_finding"
        result.detail = "agent 未找到可解释的证据"
        return result

    if config.require_grounding:
        observations = agent_result.trajectory.observations_text()
        report = grounding.check(" ".join(r.text for r in reasons), observations)
        result.grounding_ratio = report.ratio
        if not report.ok:
            result.outcome = "ungrounded"
            result.detail = "未溯源数字：" + ", ".join(report.ungrounded[:5])
            return result

    result.reasons = reasons
    return result


def apply(anomaly: AnomalyLike, outcome: AssistOutcome, *, factory: Callable | None = None) -> int:
    """Append the top-up onto the anomaly. Returns how many reasons were added.

    ``ScoredReason`` is imported lazily so this module stays importable without
    pydantic; tests inject their own factory.
    """
    if not outcome.ok:
        return 0
    if factory is None:
        from v2.monitoring.models import ScoredReason
        factory = ScoredReason

    existing = list(getattr(anomaly, "reasons", None) or [])
    for reason in outcome.reasons:
        existing.append(factory(text=reason.text, confidence=reason.confidence,
                                note=reason.note()))
    anomaly.reasons = existing
    return len(outcome.reasons)


class BudgetedAssistant:
    """Single-pass integration helper for the cron's existing loop.

    ``scripts/anomaly_to_telegram.py`` attributes and pushes each anomaly in one
    pass, so a two-pass "attribute everything, then rank, then top up" would mean
    restructuring it. It does not have to: eligibility is only known after
    attribution, but the *ranking* key is known before it, so sorting the list up
    front and spending the budget in order selects exactly the same set as
    ranking afterwards would.

    Usage in the cron — sort once, four lines inside the loop::

        assistant = BudgetedAssistant()
        for anomaly in assistant.order(anomalies):
            attribute(anomaly, fd_client=fd, memory=memory)
            assistant.maybe_assist(anomaly)          # no-op unless flagged on
            ...existing chart / caption / push...
    """

    def __init__(self, config: AssistConfig | None = None, *,
                 llm: LLMClient | None = None,
                 registry: ToolRegistry | None = None,
                 factory: Callable | None = None,
                 force: bool = False) -> None:
        self.config = config or AssistConfig()
        self.llm = llm
        self.registry = registry
        self.factory = factory
        #: ``force`` bypasses the env flag for tests and the demo runner.
        self.active = force or enabled()
        self.remaining = self.config.max_items_per_run if self.active else 0
        self.outcomes: list[AssistOutcome] = []

    @staticmethod
    def order(anomalies: list[AnomalyLike]) -> list[AnomalyLike]:
        """Highest-ranked first, so spending the budget in order is spending it well."""
        return sorted(anomalies, key=candidate_score, reverse=True)

    def maybe_assist(self, anomaly: AnomalyLike) -> AssistOutcome | None:
        """Top up this anomaly if it qualifies and budget remains. Never raises."""
        if self.remaining <= 0 or not needs_assist(anomaly):
            return None
        self.remaining -= 1
        outcome = assist(anomaly, llm=self.llm, registry=self.registry, config=self.config)
        if outcome.ok:
            apply(anomaly, outcome, factory=self.factory)
        self.outcomes.append(outcome)
        return outcome

    def summary(self) -> dict[str, Any]:
        """One line for the cron's stdout — what was spent and what came back."""
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
        return {
            "attempted": len(self.outcomes),
            "applied": sum(1 for o in self.outcomes if o.ok),
            "tokens": sum(o.tokens for o in self.outcomes),
            "elapsed_ms": sum(o.elapsed_ms for o in self.outcomes),
            "by_outcome": counts,
        }


def assist_batch(
    anomalies: list[AnomalyLike],
    *,
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    config: AssistConfig | None = None,
    factory: Callable | None = None,
) -> list[AssistOutcome]:
    """Top up the capped set of unexplained anomalies, applying what succeeds.

    Drop-in for the cron: it selects, budgets, applies and reports. Anomalies not
    selected — and top-ups that fail — are left exactly as the deterministic
    pipeline produced them.
    """
    config = config or AssistConfig()
    outcomes: list[AssistOutcome] = []
    for anomaly in select_candidates(anomalies, config):
        outcome = assist(anomaly, llm=llm, registry=registry, config=config)
        if outcome.ok:
            apply(anomaly, outcome, factory=factory)
        outcomes.append(outcome)
    return outcomes
