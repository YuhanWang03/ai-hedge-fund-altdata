"""LLM-backed planning and synthesis ports with deterministic fallbacks."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from v2.agent import presentation
from v2.agent.llm import LLMClient, LLMError
from v2.agent_v2.catalog import CapabilityCatalog
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
)
from v2.agent_v2.planning import RulePlanner
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer, synthesize_market_answer

_RESULT_CITATION = re.compile(r"\[results\.(metrics|limitations)([^\]]*)\]")
_DETAILED_ANSWER = re.compile(r"详细|完整|全面|深度|报告|逐项|表格|清单|所有|展开")
_MOVE_ANSWER = re.compile(r"(?:为什么|原因|何故).{0,12}(?:涨|跌|异动|波动)|(?:涨|跌|异动|波动).{0,12}(?:为什么|原因|怎么回事|怎么了|何故)|(?:最近|今天|今日).{0,8}(?:怎么回事|怎么了)", re.I)
_PERFORMANCE_ANSWER = re.compile(
    r"(?:最近|近期|今天|今日|本周|这周|本月|这个月|近\s*\d+\s*(?:天|日|周|月)).{0,12}(?:股价|价格|走势|表现|涨|跌|涨跌|回报|收益率)"
    r"|(?:股价|价格).{0,8}(?:走势|表现|涨跌|回报|收益率)|(?:走势|涨跌|跑赢|跑输)|(?:股票|股价|价格)?表现(?:如何|怎么样|怎样|好吗|好不好)",
    re.I,
)


def _response_intent(query: str, plan: ExecutionPlan) -> str:
    capabilities = {task.capability for task in plan.tasks}
    if "market.explain_move" in capabilities:
        return "move_explanation"
    if "market.performance" in capabilities:
        return "recent_performance"
    if _MOVE_ANSWER.search(query):
        return "move_explanation"
    if _PERFORMANCE_ANSWER.search(query):
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
量化实验必须从用户原话提取参数，不要虚构参数；未提供的参数交给工具默认值。
输出格式：{"objective":"...","tasks":[{"id":"t1","capability":"...","arguments":{},"depends_on":[],"required":true,"purpose":"..."}],"assumptions":[]}。"""
        payload = {
            "query": request.text,
            "entities": list(request.entities),
            "capabilities": capabilities,
            "maximum_tasks": self.max_tasks,
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
            if route.kind in {RouteKind.LAB, RouteKind.ASYNC} and len(tasks) > 2:
                raise ValueError("Lab plan exceeds two tasks")
            answer_mode = AnswerMode.RESEARCH_GROUNDED if route.kind == RouteKind.RESEARCH else AnswerMode.TOOL_GROUNDED
            return ExecutionPlan(
                objective=str(raw.get("objective") or request.text),
                route=route.kind,
                tasks=tasks,
                budget=_planner_budget(request, route),
                answer_mode=answer_mode,
                web_fallback_allowed=request.allow_web and route.kind == RouteKind.RESEARCH,
                assumptions=tuple(str(value) for value in raw.get("assumptions", []) if value),
                stop_conditions=("required evidence acquired", "budget exhausted", "providers unavailable"),
            )
        except (LLMError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            note = f"LLM planner fallback: {type(exc).__name__}"
            return replace(deterministic, assumptions=(*deterministic.assumptions, note))

    def _tasks(self, raw: Any, allowed: set[str]) -> tuple[PlanTask, ...]:
        if not isinstance(raw, list) or len(raw) > self.max_tasks:
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
            tasks.append(
                PlanTask(
                    id=task_id,
                    capability=capability,
                    arguments=arguments,
                    depends_on=tuple(str(value) for value in dependencies),
                    required=bool(row.get("required", True)),
                    purpose=str(row.get("purpose") or ""),
                )
            )
            seen.add(task_id)
        if any(set(task.depends_on) - seen for task in tasks):
            raise ValueError("unknown task dependency")
        return tuple(tasks)


class LLMEvidenceSynthesizer:
    """Generate prose from bounded evidence while preserving source identifiers."""

    supports_general_knowledge = True

    def __init__(
        self,
        llm: LLMClient,
        *,
        fallback: EvidenceSummarySynthesizer | None = None,
        max_context_chars: int = 28_000,
    ) -> None:
        self.llm = llm
        self.fallback = fallback or EvidenceSummarySynthesizer()
        self.max_context_chars = max(4_000, max_context_chars)

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
            system += """

再严格遵循 response_intent：
- recent_performance：先回答最新交易日、近 5 日和近 1 月的价格回报，再说明相对行业或大盘基准的强弱及成交量；不得用营收、毛利率或估值代替价格表现。若 metrics.is_intraday=true，必须写“截至查询时”或“盘中”，不能写“收盘”；当前累计成交量只能与完整日均量做进度参考，不得据此判断放量、缩量或上涨持续性。数据不足时明确缺少哪个时间窗口。
- move_explanation：第一句回答是否上涨/下跌、日期、幅度和成交量。把已确认行情事实、高置信度直接驱动、普通候选解释分开。只有 metadata.claim_role=confirmed_driver 的证据才能写成已确认原因；candidate_driver 必须写成“可能相关”并说明中/低置信度。如果 confirmed_driver_count 为 0，用自然语言说“暂未找到可核实的同日催化剂，具体触发原因尚未确认”，不要输出“0 个驱动”之类的系统字段。不能把历史涨幅、机构持仓或时间不匹配的新闻写成当日直接原因。没有直接驱动时最多展示 1 条最相关的候选线索，过于间接的线索可直接省略。必须给出行业基准对比；若工具没有基准则说明缺失。若 metrics.is_intraday=true，必须标明盘中口径，且不得用当前累计成交量推断放量/缩量或持续性。
- stock_research：围绕公司的核心投资矛盾组织答案，不逐项报分。ROIC、ROE、利润率等异常高于 100% 的比率必须提示其依赖数据与计算口径，不能当作无条件质量结论。

每个引用只支持它紧邻的那句话。不要用一条聚合引用同时支撑价格、成交量、新闻和期权等不同事实。"""
            payload = self._payload(request.text, plan, results, evidence)
        try:
            response = self.llm.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": payload},
                ],
                None,
            )
            answer = presentation.strip_deliberation(response.text)
            if not answer:
                raise ValueError("synthesizer returned an empty answer")
            answer = _normalize_result_citations(answer, results, evidence)
            market_fallback = synthesize_market_answer(results, evidence)
            if market_fallback is not None:
                from v2.agent_v2.verification import verify_answer

                report = verify_answer(answer, evidence, answer_mode=plan.answer_mode, results=results)
                if not report.ok:
                    return market_fallback
            return answer
        except (LLMError, ValueError, TypeError) as exc:
            return self.fallback.synthesize(request, plan, results, evidence)

    def _payload(self, query: str, plan: ExecutionPlan, results: list[ToolEnvelope], evidence) -> str:
        data = {
            "query": query[:2000],
            "objective": plan.objective[:2000],
            "response_style": "detailed" if _DETAILED_ANSWER.search(query) else "brief",
            "response_intent": _response_intent(query, plan),
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
