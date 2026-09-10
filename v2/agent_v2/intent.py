"""Intent: what a question asks for, as fixed fields instead of matched words.

The router and the rule planner decide from regular expressions over the
text.  That works for the wordings they have seen and breaks on the next
paraphrase.  This module puts the translation from words to a structured
intent where it belongs, with the model, and keeps the decision about
which tasks follow from an intent with rules.

Step one is shadow mode: every live question is classified in the
background, the model's intent is written next to the one implied by the
regex decision, and ``python -m v2.agent_v2.eval.intent_report`` shows
where they agree and disagree.  Nothing about the run changes until the
agreement data says the classifier can drive routing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from v2.agent_v2.models import ExecutionPlan, NormalizedRequest, RouteDecision, RouteKind

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _PROJECT_ROOT / "data" / "agent_v2_intents.jsonl"

#: What kind of answer the question wants.
KINDS = ("research", "lookup", "command", "knowledge", "lab", "help")
#: The time the question is about.
SCOPES = ("today", "recent", "window", "since_purchase", "none")
#: Direction of the move the question is about.
DIRECTIONS = ("up", "down", "none")
#: What the answer must contain.  Each maps to one or more capabilities in
#: the planner's templates; the classifier picks from this list only.
WANTS = (
    "attribution",  # why a stock moved
    "news",  # recent news / events
    "filings",  # SEC filings, insider forms
    "earnings",  # results, EPS, earnings dates
    "valuation",
    "ownership",  # insiders, institutions
    "supply_chain",
    "catalysts",
    "risk",
    "compare",  # several stocks against each other
    "performance",  # returns, volume, price action
    "drawdown",  # a stretch of decline
    "runup",  # a stretch of gains
    "ranking",  # which holding is best / worst
    "portfolio",  # positions, weights, P/L
    "watchlist",
    "alerts",
    "settings",
    "macro",
    "macro_release",  # a specific data release (CPI, FOMC, ...)
    "guru",  # a manager's 13F
    "ark",
    "research_changes",  # what changed since the last research
    "briefing",  # what should I know today
    "full",  # a complete research report
    "overview",
)

_SYSTEM = """你是投研助手的意图分类器，只输出 JSON，不回答问题。
把用户的一句话归成固定字段，字段值只能从给定枚举里选：
kind：research（需要研究、分析、比较、判断值不值得买）| lookup（查一个事实：行情、持仓、财报日期、列表）| command（改用户状态：加关注、设提醒、删除）| knowledge（概念解释，不涉及具体股票或账户）| lab（回测、参数扫描、事件研究、筛选）| help（问助手能做什么）
scope：today（今天、盘中、昨天）| recent（最近、这周、这个月，没有明确起点）| window（明确区间：今年、一年、从高点以来）| since_purchase（买入以来、建仓以来）| none
direction：up | down | none（问题针对的是上涨还是下跌）
wants：从列表里选 0 到 4 个，按重要性排序：%s
tickers：问题里的股票代码（大写），公司中文名或英文名要转成代码；没有就空数组。
portfolio_scope：问题是否指向用户自己的持仓、关注列表或账户（true/false）。
confidence：0 到 1。
只输出：{"kind":"...","scope":"...","direction":"...","wants":[...],"tickers":[...],"portfolio_scope":false,"confidence":0.9}""" % "、".join(WANTS)


@dataclass
class Intent:
    kind: str = "lookup"
    scope: str = "none"
    direction: str = "none"
    wants: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    portfolio_scope: bool = False
    confidence: float | None = None
    #: ``rules`` (projected from the regex decision) or ``model``.
    source: str = "rules"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["wants"] = list(self.wants)
        data["tickers"] = list(self.tickers)
        return data


def parse_intent(raw: Any, *, source: str = "model") -> Intent:
    """Validate a classifier reply against the fixed vocabulary; unknown values are dropped, not guessed."""

    if not isinstance(raw, dict):
        raise ValueError("intent must be an object")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    scope = str(raw.get("scope") or "none").strip().lower()
    direction = str(raw.get("direction") or "none").strip().lower()
    wants = tuple(dict.fromkeys(str(value).strip().lower() for value in (raw.get("wants") or []) if str(value).strip().lower() in WANTS))[:4]
    tickers = tuple(dict.fromkeys(str(value).strip().upper() for value in (raw.get("tickers") or []) if re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,7}", str(value).strip())))[:8]
    confidence = raw.get("confidence")
    try:
        confidence = None if confidence is None else max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None
    return Intent(
        kind=kind,
        scope=scope if scope in SCOPES else "none",
        direction=direction if direction in DIRECTIONS else "none",
        wants=wants,
        tickers=tickers,
        portfolio_scope=bool(raw.get("portfolio_scope")),
        confidence=confidence,
        source=source,
        note=str(raw.get("note") or "")[:120],
    )


class IntentClassifier:
    """One model call that turns the question into an :class:`Intent`; ``None`` when the model fails or answers off-vocabulary."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    def classify(self, request: NormalizedRequest) -> Intent | None:
        if self.llm is None:
            return None
        from v2.agent_v2.agents.base import strip_fence
        from v2.usage_context import usage_source

        payload = json.dumps({"text": request.text, "entities": list(request.entities)}, ensure_ascii=False)
        try:
            with usage_source("agent_v2.intent"):
                response = self.llm.complete([{"role": "system", "content": _SYSTEM}, {"role": "user", "content": payload}], None)
            return parse_intent(json.loads(strip_fence(response.text)))
        except Exception as exc:  # noqa: BLE001 — a failed classification is a ledger row, never a failed run
            logger.warning("intent classifier failed: %s: %s", type(exc).__name__, exc)
            return None


# -- the regex decision, projected onto the same fields ------------------------

_FOCUS_WANTS = {"valuation": "valuation", "filings": "filings", "earnings": "earnings", "ownership": "ownership", "market": "performance", "supply_chain": "supply_chain", "catalysts": "catalysts", "risk": "risk", "full": "full", "overview": "overview", "fundamentals": "overview"}
_CAPABILITY_WANTS = {
    "research.compare": ("compare",),
    "research.changes": ("research_changes",),
    "market.performance": ("performance",),
    "market.drawdown": ("drawdown",),
    "market.runup": ("runup",),
    "market.explain_move": ("attribution",),
    "market.attribute_move": ("attribution",),
    "market.anomaly_history": (),
    "filings.recent": ("filings",),
    "filings.read_events": ("filings",),
    "web.research": ("news",),
    "account.portfolio": ("portfolio",),
    "account.performance": ("portfolio", "performance"),
    "account.risk": ("risk", "portfolio"),
    "account.earnings_schedule": ("earnings",),
    "macro.overview": ("macro",),
    "macro.release": ("macro_release",),
    "institutional.manager_portfolio": ("guru",),
    "etf.ark_activity": ("ark",),
}
_HELP = re.compile(r"你能帮我做什么|你能做什么|能做什么|有什么功能|会做什么|怎么用|如何使用")


def rule_intent(request: NormalizedRequest, decision: RouteDecision, plan: ExecutionPlan) -> Intent:
    """What the router and the rule planner decided, in intent fields, so the two sides can be compared."""

    wants: list[str] = []
    scope = "none"
    direction = "none"
    portfolio = False
    for task in plan.tasks:
        for want in _CAPABILITY_WANTS.get(task.capability, ()):
            if want not in wants:
                wants.append(want)
        if task.capability == "research.stock":
            want = _FOCUS_WANTS.get(str(task.arguments.get("focus") or "overview"), "overview")
            if want not in wants:
                wants.append(want)
        if task.capability == "state.read":
            section = str(task.arguments.get("section") or "")
            want = {"alerts": "alerts", "settings": "settings", "watchlist": "watchlist"}.get(section)
            if want and want not in wants:
                wants.append(want)
            portfolio = True
        if task.capability.startswith("account."):
            portfolio = True
        if task.capability == "market.explain_move" and not task.arguments.get("day"):
            scope = "today"
        if task.capability == "market.attribute_move" and task.arguments.get("day"):
            scope = "window"
        if task.capability == "market.drawdown":
            direction = "down"
        if task.capability == "market.runup":
            direction = "up"
        if task.fan_out:
            if "ranking" not in wants:
                wants.append("ranking")
            portfolio = True
    frame = plan.frame or {}
    if frame.get("kind") == "drawdown":
        direction, scope = "down", "since_purchase" if "买入" in request.text or "建仓" in request.text else "window"
    elif frame.get("kind") == "runup":
        direction, scope = "up", "since_purchase" if "买入" in request.text or "建仓" in request.text else "window"
    elif scope == "none" and any(task.capability in {"market.performance", "web.research", "filings.recent"} for task in plan.tasks):
        scope = "recent"
    if plan.direct_answer and _HELP.search(request.text):
        kind = "help"
    elif decision.kind == RouteKind.GENERAL_KNOWLEDGE:
        kind = "knowledge"
    elif decision.kind == RouteKind.COMMAND or plan.requires_confirmation:
        kind = "command"
    elif decision.kind in {RouteKind.LAB, RouteKind.ASYNC}:
        kind = "lab"
    elif decision.kind == RouteKind.RESEARCH:
        kind = "research"
    else:
        kind = "lookup"
    return Intent(kind=kind, scope=scope, direction=direction, wants=tuple(wants[:4]), tickers=tuple(request.entities), portfolio_scope=portfolio, source="rules")


COMPARED_FIELDS = ("kind", "scope", "direction", "wants", "tickers", "portfolio_scope")


def compare_intents(rules: Intent, model: Intent) -> dict[str, bool]:
    """Per field: do the two sides agree?  ``wants`` agrees when the model's first want is among the rules' wants (or both are empty)."""

    return {
        "kind": rules.kind == model.kind,
        "scope": rules.scope == model.scope,
        "direction": rules.direction == model.direction,
        "wants": (not rules.wants and not model.wants) or bool(model.wants and model.wants[0] in rules.wants) or bool(rules.wants and rules.wants[0] in model.wants),
        "tickers": set(rules.tickers) == set(model.tickers),
        "portfolio_scope": rules.portfolio_scope == model.portfolio_scope,
    }


# -- shadow ledger --------------------------------------------------------------


def ledger_path() -> Path:
    return Path(os.environ.get("AGENT_V2_INTENT_LEDGER") or _DEFAULT_PATH)


def shadow_row(request: NormalizedRequest, decision: RouteDecision, plan: ExecutionPlan, model: Intent | None, *, run_id: str, elapsed_ms: int, channel: str = "") -> dict[str, Any]:
    rules = rule_intent(request, decision, plan)
    row: dict[str, Any] = {
        "at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "channel": channel,
        "text": (request.original_text or request.text or "")[:200],
        "route": decision.kind.value,
        "capabilities": [task.capability for task in plan.tasks],
        "rules": rules.to_dict(),
        "model": model.to_dict() if model else None,
        "agree": compare_intents(rules, model) if model else None,
        "elapsed_ms": elapsed_ms,
    }
    return row


def record_shadow(row: dict[str, Any], path: Path | None = None) -> bool:
    target = path or ledger_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("intent ledger not written (%s): %s", target, exc)
        return False
    return True


def read_shadow(path: Path | None = None, *, since_days: int | None = None) -> list[dict[str, Any]]:
    target = path or ledger_path()
    if not target.exists():
        return []
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=since_days)).isoformat() if since_days else ""
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and (not cutoff or str(row.get("at") or "") >= cutoff):
                rows.append(row)
    return rows


def shadow_classify(classifier: IntentClassifier, request: NormalizedRequest, decision: RouteDecision, plan: ExecutionPlan, *, run_id: str, channel: str = "", path: Path | None = None) -> dict[str, Any]:
    """Classify, compare with the rules and append the row; returns the row (for tests and inline callers)."""

    started = time.monotonic()
    model = classifier.classify(request)
    row = shadow_row(request, decision, plan, model, run_id=run_id, elapsed_ms=int((time.monotonic() - started) * 1000), channel=channel)
    record_shadow(row, path)
    return row


# -- report ---------------------------------------------------------------------


def agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement rate per field, the model's failure count, and every disagreement with its text."""

    classified = [row for row in rows if row.get("model")]
    failed = len(rows) - len(classified)
    per_field: dict[str, dict[str, int]] = {name: {"agree": 0, "total": 0} for name in COMPARED_FIELDS}
    disagreements: list[dict[str, Any]] = []
    for row in classified:
        agree = row.get("agree") or {}
        wrong = [name for name in COMPARED_FIELDS if not agree.get(name)]
        for name in COMPARED_FIELDS:
            per_field[name]["total"] += 1
            per_field[name]["agree"] += 1 if agree.get(name) else 0
        if wrong:
            disagreements.append({"text": row.get("text"), "fields": wrong, "rules": row.get("rules"), "model": row.get("model"), "route": row.get("route"), "capabilities": row.get("capabilities")})
    full = sum(1 for row in classified if all((row.get("agree") or {}).get(name) for name in COMPARED_FIELDS))
    return {
        "rows": len(rows),
        "classified": len(classified),
        "failed": failed,
        "full_agreement": full,
        "full_agreement_rate": round(full / len(classified), 3) if classified else None,
        "fields": {name: {**counts, "rate": round(counts["agree"] / counts["total"], 3) if counts["total"] else None} for name, counts in per_field.items()},
        "elapsed_ms_avg": round(sum(int(row.get("elapsed_ms") or 0) for row in classified) / len(classified)) if classified else 0,
        "disagreements": disagreements,
    }


def _short(intent: dict[str, Any] | None) -> str:
    if not intent:
        return "—"
    parts = [str(intent.get("kind")), str(intent.get("scope")), str(intent.get("direction")), "+".join(intent.get("wants") or []) or "∅", ",".join(intent.get("tickers") or []) or "∅"]
    if intent.get("portfolio_scope"):
        parts.append("账户")
    return " / ".join(parts)


def render(summary: dict[str, Any], *, since_days: int | None) -> str:
    lines = [f"# 意图分类影子报告{f'（近 {since_days} 天）' if since_days else ''}", ""]
    if not summary["rows"]:
        lines.append("账本里还没有影子分类记录。")
        return "\n".join(lines)
    lines.append(f"问题 {summary['rows']} 个，模型给出有效分类 {summary['classified']} 个，失败 {summary['failed']} 个，平均 {summary['elapsed_ms_avg']} ms。")
    if summary["full_agreement_rate"] is not None:
        lines.append(f"六个字段全部一致：{summary['full_agreement']} / {summary['classified']}（{summary['full_agreement_rate']:.0%}）。")
    lines.append("")
    lines.append("| 字段 | 一致 | 一致率 |")
    lines.append("|---|---|---|")
    for name, counts in summary["fields"].items():
        rate = "—" if counts["rate"] is None else f"{counts['rate']:.0%}"
        lines.append(f"| {name} | {counts['agree']} / {counts['total']} | {rate} |")
    if summary["disagreements"]:
        lines.append("")
        lines.append("| 不一致的问题 | 字段 | 规则 | 模型 |")
        lines.append("|---|---|---|---|")
        for entry in summary["disagreements"]:
            lines.append(f"| {entry['text']} | {'、'.join(entry['fields'])} | {_short(entry['rules'])} | {_short(entry['model'])} |")
        lines.append("")
        lines.append("规则/模型列：kind / scope / direction / wants / tickers（∅ = 空）。规则侧是从路由和计划反推的字段，不一致不等于模型错，要逐条看。")
    return "\n".join(lines)
