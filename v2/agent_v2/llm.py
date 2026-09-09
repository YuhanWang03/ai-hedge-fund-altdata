"""LLM-backed planning and synthesis ports with deterministic fallbacks."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from v2.agent import presentation
from v2.agent.llm import LLMClient, LLMError
from v2.agent_v2.catalog import CapabilityCatalog, default_catalog
from v2.agent_v2.execution import task_limit
from v2.agent_v2.models import (
    AnswerMode,
    BudgetClass,
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    PlanTask,
    RouteDecision,
    RouteKind,
    ToolEnvelope,
    VerificationReport,
)
from v2.agent_v2.planning import RulePlanner
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer

_RESULT_CITATION = re.compile(r"\[results\.(metrics|limitations)([^\]]*)\]")
_DETAILED_ANSWER = re.compile(r"详细|完整|全面|深度|报告|逐项|表格|清单|所有|展开")


def _response_intent(plan: ExecutionPlan, results: list[ToolEnvelope]) -> str:
    """A coarse label derived from the capabilities that actually ran."""

    capabilities = {task.capability for task in plan.tasks} | {result.capability for result in results}
    if "market.explain_move" in capabilities:
        return "move_explanation"
    if "market.performance" in capabilities:
        return "recent_performance"
    return "stock_research"


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        value = "\n".join(lines).strip()
    start, end = value.find("{"), value.rfind("}")
    return value[start : end + 1] if start >= 0 and end > start else value


def _planner_budget(request: NormalizedRequest, route: RouteDecision) -> BudgetClass:
    if route.kind == RouteKind.ASYNC:
        return BudgetClass.DEEP
    if route.kind == RouteKind.LAB:
        return BudgetClass.LAB
    if any(word in request.text for word in ("持仓", "组合", "账户")):
        return BudgetClass.PORTFOLIO
    if len(request.entities) >= 2:
        return BudgetClass.COMPARISON
    return BudgetClass.STANDARD


class StructuredLLMPlanner:
    """Use an LLM for research/Lab planning, then validate before execution."""

    def __init__(
        self,
        llm: LLMClient,
        catalog: CapabilityCatalog,
        *,
        fallback: RulePlanner | None = None,
        max_tasks: int = 7,
    ) -> None:
        self.llm = llm
        self.catalog = catalog
        self.fallback = fallback or RulePlanner()
        self.max_tasks = max(1, max_tasks)

    def plan(self, request: NormalizedRequest, route: RouteDecision) -> ExecutionPlan:
        deterministic = self.fallback.plan(request, route)
        if deterministic.tasks and deterministic.tasks[0].capability in {"market.performance", "market.explain_move"}:
            return deterministic
        if route.kind not in {RouteKind.RESEARCH, RouteKind.LAB, RouteKind.ASYNC}:
            return deterministic
        budget = _planner_budget(request, route)
        limit = min(self.max_tasks, task_limit(budget))
        if route.kind in {RouteKind.LAB, RouteKind.ASYNC}:
            limit = min(limit, 2)
        allowed = self.catalog.specs(route.packs)
        capabilities = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec in allowed
            if not spec.mutating
        ]
        system = """你是投研与量化实验任务规划器，只输出 JSON，不回答用户问题。
把目标拆成最少数量的现有 capability。不要编造 capability，不要安排写操作。
相同信息只获取一次；多股票比较优先 research.compare；账户问题先取账户事实。
需要对持仓或关注列表里的每只股票分别执行某个 capability 时，先安排 account.portfolio 或 state.read，再安排一个带 fan_out 的任务：
{"id":"t2","capability":"research.stock","arguments":{"focus":"filings"},"depends_on":["t1"],"fan_out":{"from":"t1","field":"tickers","argument":"ticker","max":6}}。
量化实验必须从用户原话提取参数，不要虚构参数；未提供的参数交给工具默认值。
输出格式：{"objective":"...","tasks":[{"id":"t1","capability":"...","arguments":{},"depends_on":[],"required":true,"purpose":"...","fan_out":null}],"assumptions":[]}。"""
        payload = {
            "query": request.text,
            "entities": list(request.entities),
            "capabilities": capabilities,
            "maximum_tasks": limit,
        }
        try:
            response = self.llm.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                None,
            )
            raw = json.loads(_strip_fence(response.text))
            tasks = self._tasks(raw.get("tasks"), {spec.name for spec in allowed})
            if not tasks:
                raise ValueError("planner returned no executable tasks")
            assumptions = [str(value) for value in raw.get("assumptions", []) if value]
            trimmed = _trim_to_budget(tasks, limit)
            if len(trimmed) < len(tasks):
                dropped = ", ".join(task.capability for task in tasks if task not in trimmed)
                assumptions.append(f"Planner trimmed {len(tasks) - len(trimmed)} task(s) to the {budget.value} budget of {limit}: {dropped}")
                tasks = trimmed
            answer_mode = AnswerMode.RESEARCH_GROUNDED if route.kind == RouteKind.RESEARCH else AnswerMode.TOOL_GROUNDED
            return ExecutionPlan(
                objective=str(raw.get("objective") or request.text),
                route=route.kind,
                tasks=tasks,
                budget=budget,
                answer_mode=answer_mode,
                web_fallback_allowed=request.allow_web and route.kind == RouteKind.RESEARCH,
                assumptions=tuple(assumptions),
            )
        except (LLMError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            note = f"LLM planner fallback: {type(exc).__name__}"
            return replace(deterministic, assumptions=(*deterministic.assumptions, note))

    def _tasks(self, raw: Any, allowed: set[str]) -> tuple[PlanTask, ...]:
        # Over-long lists are trimmed to the budget after parsing; only an
        # absurd list is treated as a malformed plan.
        if not isinstance(raw, list) or len(raw) > 4 * self.max_tasks:
            raise ValueError("invalid task list")
        tasks: list[PlanTask] = []
        seen: set[str] = set()
        for index, row in enumerate(raw, 1):
            if not isinstance(row, dict):
                raise ValueError("task must be an object")
            capability = str(row.get("capability") or "")
            if capability not in allowed:
                raise ValueError(f"capability is not allowed: {capability}")
            task_id = str(row.get("id") or f"t{index}")
            if task_id in seen:
                raise ValueError("duplicate task id")
            arguments = row.get("arguments") or {}
            dependencies = row.get("depends_on") or []
            if not isinstance(arguments, dict) or not isinstance(dependencies, list):
                raise ValueError("invalid task arguments or dependencies")
            fan_out = row.get("fan_out") or None
            if fan_out is not None:
                if not isinstance(fan_out, dict) or not fan_out.get("from") or not fan_out.get("argument"):
                    raise ValueError("invalid fan_out")
                fan_out = {"from": str(fan_out["from"]), "field": str(fan_out.get("field") or "tickers"), "argument": str(fan_out["argument"]), "max": int(fan_out.get("max") or 6)}
                dependencies = list(dict.fromkeys([*dependencies, fan_out["from"]]))
            tasks.append(
                PlanTask(
                    id=task_id,
                    capability=capability,
                    arguments=arguments,
                    depends_on=tuple(str(value) for value in dependencies),
                    required=bool(row.get("required", True)),
                    purpose=str(row.get("purpose") or ""),
                    fan_out=fan_out,
                )
            )
            seen.add(task_id)
        if any(set(task.depends_on) - seen for task in tasks):
            raise ValueError("unknown task dependency")
        return tuple(tasks)


def _trim_to_budget(tasks: tuple[PlanTask, ...], limit: int) -> tuple[PlanTask, ...]:
    """Drop optional tasks from the end first, then required ones, then dangling dependents."""

    kept = list(tasks)
    while len(kept) > limit:
        optional = [task for task in kept if not task.required]
        kept.remove(optional[-1] if optional else kept[-1])
    while True:
        ids = {task.id for task in kept}
        dangling = [task for task in kept if set(task.depends_on) - ids]
        if not dangling:
            return tuple(kept)
        kept = [task for task in kept if task not in dangling]


class LLMEvidenceSynthesizer:
    """Generate prose from bounded evidence while preserving source identifiers."""

    supports_general_knowledge = True

    def __init__(
        self,
        llm: LLMClient,
        *,
        catalog: CapabilityCatalog | None = None,
        fallback: EvidenceSummarySynthesizer | None = None,
        max_context_chars: int = 28_000,
    ) -> None:
        self.llm = llm
        self.catalog = catalog or default_catalog()
        self.fallback = fallback or EvidenceSummarySynthesizer()
        self.max_context_chars = max(4_000, max_context_chars)
        #: Diagnostic only: how the most recent answer was produced
        #: (``clean``, ``repaired``, ``fallback`` or ``knowledge``).  Written
        #: per call without locking; evaluation reads it, production ignores it.
        self.last_outcome = ""

    def synthesize(self, request, plan, results, evidence) -> str:
        if plan.answer_mode == AnswerMode.GENERAL_KNOWLEDGE:
            system = """用中文回答稳定的金融概念和分析方法。明确这是通用知识回答，未使用实时数据。
不要声称知道当前股价、最新财报、近期新闻、用户持仓或其他可能变化的事实。"""
            payload = request.text
        else:
            system = """你是证据约束的中文投研助手，表达要像一位清楚、克制、有判断力的研究同事。
只能陈述输入证据支持的外部事实。每项关键事实后必须写对应的 [evidence_id]。
方括号内只能原样使用 evidence 数组中真实存在的 id；严禁把 results.*、字段路径、source_id 或占位符当作引用。
results 中的评分或限制如需引用，使用 evidence 中 citation_kind 为 metrics 或 limitations 的对应条目。
推断必须标成“推断”；数据缺失必须明确说明。不得把一个主体的数据归给另一个主体。
不要把历史回测写成未来收益保证。不要输出未在证据中出现的数字。

严格遵循输入中的 response_style：
- brief：直接给判断，用 3—5 个短段落、约 300—500 个中文字完成回答。挑选最有决策价值的 3—5 条事实，只讲一个主要风险和最重要的数据缺口，最后指出接下来值得观察什么。不要使用标题、表格、分隔线、编号清单、“正面/负面/中性”标签、“必须说明”或单独的免责声明章节；不要重复同一事实。
- detailed：用户明确要求详细、完整、全面、表格或逐项展开时，才允许使用小标题与列表，但仍应合并重复内容并保持自然。

把 BULLISH、MEDIUM、forward_pe、revision_trend 等内部英文标签翻译或解释成自然中文；必要的通用缩写可以保留。不要逐项复述所有模块，也不要把工具输出改写成机械评分单。"""
            guidance = self._guidance(plan, results)
            if guidance:
                system += "\n\n再严格遵循以下与本次所用能力对应的 response_intent 规则：\n" + guidance
            system += "\n\n每个引用只支持它紧邻的那句话。不要用一条聚合引用同时支撑价格、成交量、新闻和期权等不同事实。"
            payload = self._payload(request.text, plan, results, evidence)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ]
        self.last_outcome = "fallback"
        try:
            answer = self._draft(messages, results, evidence)
            if plan.answer_mode == AnswerMode.GENERAL_KNOWLEDGE:
                self.last_outcome = "knowledge"
                return answer
            from v2.agent_v2.verification import verify_answer

            report = verify_answer(answer, evidence, answer_mode=plan.answer_mode, results=results)
            if report.ok:
                self.last_outcome = "clean"
                return answer
            # One repair round: the verifier names what failed and what must
            # stay; a second failure falls back to deterministic prose rather
            # than shipping flagged numbers to the user.
            repair = self._draft(
                [
                    *messages,
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": repair_instruction(report)},
                ],
                results,
                evidence,
            )
            if verify_answer(repair, evidence, answer_mode=plan.answer_mode, results=results).ok:
                self.last_outcome = "repaired"
                return repair
        except (LLMError, ValueError, TypeError):
            pass
        return self.fallback.synthesize(request, plan, results, evidence)

    def _guidance(self, plan: ExecutionPlan, results: list[ToolEnvelope]) -> str:
        """Collect the adapters' own answer rules for the capabilities in play."""

        names: list[str] = []
        for name in (*(task.capability for task in plan.tasks), *(result.capability for result in results)):
            if name not in names:
                names.append(name)
        lines: list[str] = []
        for name in names:
            spec = self.catalog.get(name)
            if spec is not None and spec.answer_guidance and spec.answer_guidance not in lines:
                lines.append(spec.answer_guidance)
        return "\n".join(f"- {line}" for line in lines)

    def _draft(self, messages: list[dict[str, str]], results: list[ToolEnvelope], evidence: list[EvidenceItem]) -> str:
        response = self.llm.complete(messages, None)
        answer = presentation.strip_deliberation(response.text)
        if not answer:
            raise ValueError("synthesizer returned an empty answer")
        return _normalize_result_citations(answer, results, evidence)

    def _payload(self, query: str, plan: ExecutionPlan, results: list[ToolEnvelope], evidence) -> str:
        data = {
            "query": query[:2000],
            "objective": plan.objective[:2000],
            "response_style": "detailed" if _DETAILED_ANSWER.search(query) else "brief",
            "response_intent": _response_intent(plan, results),
            "assumptions": list(plan.assumptions),
            "results": [
                {
                    "capability": result.capability,
                    "status": result.status.value,
                    "subject": result.subject,
                    "as_of": result.as_of,
                    "summary": result.summary[:2500],
                    "metrics": result.metrics,
                    "findings": result.findings[:8],
                    "limitations": result.limitations[:8],
                    "errors": result.errors[:3],
                }
                for result in results
            ],
            "evidence": [item.to_dict() for item in evidence[:40]],
        }
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) <= self.max_context_chars:
            return encoded
        # Preserve the question, summaries, and evidence heads rather than
        # allowing transport-level truncation to cut an arbitrary JSON token.
        data["evidence"] = data["evidence"][:16]
        for result in data["results"]:
            result["findings"] = result["findings"][:3]
            result["summary"] = result["summary"][:1000]
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) <= self.max_context_chars:
            return encoded
        data["results"] = [
            {
                "capability": row["capability"],
                "status": row["status"],
                "subject": row["subject"],
                "summary": row["summary"][:400],
                "limitations": row["limitations"][:2],
            }
            for row in data["results"]
        ]
        data["evidence"] = [
            {
                "id": row["id"],
                "entity": row["entity"],
                "claim": str(row["claim"])[:500],
                "value": row["value"],
                "as_of": row["as_of"],
                "source_id": row["source_id"],
            }
            for row in data["evidence"][:12]
        ]
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        while len(encoded) > self.max_context_chars and data["evidence"]:
            data["evidence"].pop()
            encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) > self.max_context_chars:
            data = {
                "query": query[:800],
                "objective": plan.objective[:400],
                "results": [
                    {
                        "capability": row["capability"],
                        "status": row["status"],
                        "subject": row["subject"],
                        "summary": row["summary"][:120],
                    }
                    for row in data["results"]
                ],
                "evidence": [],
                "truncated": True,
            }
            encoded = json.dumps(data, ensure_ascii=False, default=str)
        return encoded


def repair_instruction(report: VerificationReport) -> str:
    """Tell the model exactly what failed and what must survive the rewrite."""

    lines = ["校验未通过，请重写完整回答。"]
    if report.ungrounded_numbers:
        lines.append(
            "以下数字在本轮证据中找不到：" + "、".join(report.ungrounded_numbers[:12]) + "。"
            "只能使用证据中出现的数字；若是你自己的计算，请把算式完整写出（例如 22.4% + 18.2% = 40.6%）；无法支持的数字直接删掉，宁可省略也不要编造。"
        )
    if report.unknown_citations:
        lines.append("以下引用 id 不存在：" + "、".join(report.unknown_citations[:12]) + "。方括号内只能原样使用 evidence 数组中真实存在的 id。")
    for warning in report.warnings[:6]:
        lines.append(f"其他问题：{warning}。")
    if report.traced_numbers:
        lines.append("以下数字已通过校验，必须原样保留：" + "、".join(report.traced_numbers)[:400] + "。")
    lines.append("请重新输出完整回答（所有段落），你的回复将完整替换初稿，是用户唯一会看到的文本。")
    return "\n".join(lines)


def _normalize_result_citations(
    answer: str,
    results: list[ToolEnvelope],
    evidence: list[EvidenceItem],
) -> str:
    """Resolve valid result-field references to their citeable derived evidence."""

    def replace_result_path(match: re.Match[str]) -> str:
        kind = match.group(1)
        suffix = match.group(2).replace("\\_", "_")
        candidates: list[EvidenceItem] = []
        for item in evidence:
            if item.metadata.get("citation_kind") != kind:
                continue
            matching_results = [result for result in results if not item.producer_run_id or result.run_id == item.producer_run_id]
            if kind == "metrics" and not any(_has_metric_path(result.metrics, suffix) for result in matching_results):
                continue
            if kind == "limitations" and (suffix or not any(result.limitations for result in matching_results)):
                continue
            candidates.append(item)
        return f"[{candidates[0].id}]" if len(candidates) == 1 else match.group(0)

    return _RESULT_CITATION.sub(replace_result_path, answer)


def _has_metric_path(metrics: dict[str, Any], suffix: str) -> bool:
    value: Any = metrics
    path = suffix.removeprefix(".")
    if not path:
        return bool(metrics)
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return value is not None
