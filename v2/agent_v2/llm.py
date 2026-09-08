"""LLM-backed planning and synthesis ports with deterministic fallbacks."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from v2.agent import presentation
from v2.agent.llm import LLMClient, LLMError
from v2.agent_v2.catalog import CapabilityCatalog
from v2.agent_v2.models import (
    AnswerMode,
    BudgetClass,
    ExecutionPlan,
    NormalizedRequest,
    PlanTask,
    RouteDecision,
    RouteKind,
    ToolEnvelope,
)
from v2.agent_v2.planning import RulePlanner
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer


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
            system = """你是证据约束的投研回答器。先给结论，再给依据和限制。
只能陈述输入证据支持的外部事实。每项关键事实后必须写对应的 [evidence_id]。
推断必须标成“推断”；数据缺失必须明确说明。不得把一个主体的数据归给另一个主体。
不要把历史回测写成未来收益保证。不要输出未在证据中出现的数字。"""
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
            return answer
        except (LLMError, ValueError, TypeError) as exc:
            return self.fallback.synthesize(request, plan, results, evidence)

    def _payload(self, query: str, plan: ExecutionPlan, results: list[ToolEnvelope], evidence) -> str:
        data = {
            "query": query[:2000],
            "objective": plan.objective[:2000],
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
