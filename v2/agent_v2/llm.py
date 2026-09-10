"""LLM-backed planning and synthesis ports with deterministic fallbacks."""

from __future__ import annotations

import json
import logging
import re
import threading
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
from v2.agent_v2.planning import RulePlanner, portfolio_ranking
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer
from v2.agent_v2.verification import locate_number

logger = logging.getLogger(__name__)

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
        if deterministic.tasks and deterministic.tasks[0].capability in {"market.performance", "market.explain_move", "web.research"}:
            return deterministic
        # Rules also own a ranking of the user's holdings: the position card
        # answers it, and the model tends to fan out over every holding.
        capabilities = {task.capability for task in deterministic.tasks}
        if "account.portfolio" in capabilities and portfolio_ranking(request.text) and capabilities <= {"account.portfolio", "account.performance", "market.explain_move", "market.performance"}:
            return deterministic
        # A follow-up the session framed (a loss since purchase) has a rule
        # plan built around that frame; the model would plan the bare words.
        if any(str(note).startswith("context_frame:") for note in deterministic.assumptions):
            return deterministic
        # Rules own market questions and non-thin lookups; the model gets
        # research and lab routes, plus lookups the rules could not resolve
        # beyond a scope read (the holdout wording the rules never saw).
        thin = all(task.capability in {"account.portfolio", "state.read"} for task in deterministic.tasks)
        thin_lookup = route.kind == RouteKind.FAST_LOOKUP and thin and not deterministic.direct_answer
        if route.kind not in {RouteKind.RESEARCH, RouteKind.LAB, RouteKind.ASYNC} and not thin_lookup:
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
{"id":"t2","capability":"research.stock","arguments":{"focus":"filings"},"depends_on":["t1"],"fan_out":{"from":"t1","field":"tickers","argument":"ticker","max":8}}。
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
            if thin_lookup and deterministic.tasks:
                # A thin rule plan is a floor, not a draft: keep its scope reads
                # and let the model add what the wording implies on top.
                tasks = _merge_tasks(deterministic.tasks, tasks)
            tasks = _inherit_fan_out_rank(tasks, deterministic.tasks)
            trimmed = _trim_to_budget(tasks, limit)
            if len(trimmed) < len(tasks):
                dropped = ", ".join(task.capability for task in tasks if task not in trimmed)
                assumptions.append(f"Planner trimmed {len(tasks) - len(trimmed)} task(s) to the {budget.value} budget of {limit}: {dropped}")
                tasks = trimmed
            grounded = route.kind == RouteKind.RESEARCH or any(task.capability.startswith(("research.", "market.")) for task in tasks)
            answer_mode = AnswerMode.RESEARCH_GROUNDED if grounded else AnswerMode.TOOL_GROUNDED
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
                fan_out = {"from": str(fan_out["from"]), "field": str(fan_out.get("field") or "tickers"), "argument": str(fan_out["argument"]), "max": int(fan_out.get("max") or 8)}
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


def _inherit_fan_out_rank(tasks: tuple[PlanTask, ...], deterministic: tuple[PlanTask, ...]) -> tuple[PlanTask, ...]:
    """Carry the rules' fan-out ordering onto the model's plan.

    The rules read the ranking intent of the wording ("跌得最多" orders by
    P/L); the model's fan-out over the same source inherits it so the cap
    keeps the relevant holdings.
    """

    ranks: dict[str, dict] = {}
    by_id = {task.id: task for task in deterministic}
    for task in deterministic:
        rank = (task.fan_out or {}).get("rank")
        source = by_id.get(str((task.fan_out or {}).get("from") or ""))
        if isinstance(rank, dict) and source is not None:
            ranks[source.capability] = rank
    if not ranks:
        return tasks
    ids = {task.id: task for task in tasks}
    updated: list[PlanTask] = []
    for task in tasks:
        fan_out = task.fan_out
        if fan_out and "rank" not in fan_out:
            source = ids.get(str(fan_out.get("from") or ""))
            rank = ranks.get(source.capability) if source is not None else None
            if rank is not None:
                fan_out = {**fan_out, "rank": dict(rank)}
                task = replace(task, fan_out=fan_out)
        updated.append(task)
    return tuple(updated)


def _merge_tasks(base: tuple[PlanTask, ...], extra: tuple[PlanTask, ...]) -> tuple[PlanTask, ...]:
    """Append model-proposed tasks that the rule plan does not already contain."""

    merged = list(base)
    seen = {(task.capability, json.dumps(task.arguments, sort_keys=True, default=str)) for task in base}
    ids = {task.id for task in base}
    for task in extra:
        key = (task.capability, json.dumps(task.arguments, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        task_id = task.id
        while task_id in ids:
            task_id = f"{task_id}-llm"
        ids.add(task_id)
        depends = tuple(dep if dep in ids else dep for dep in task.depends_on)
        merged.append(replace(task, id=task_id, depends_on=depends))
    return tuple(merged)


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
        # Per-thread diagnostics of the most recent call: the web backend and
        # the benchmark both run several answers at once on one synthesizer.
        self._diagnostics = threading.local()

    def _reset_diagnostics(self) -> None:
        self._diagnostics.outcome = "fallback"
        self._diagnostics.draft = ""
        self._diagnostics.attempts = []
        self._diagnostics.completions = []
        self._diagnostics.drafts = []

    @property
    def last_outcome(self) -> str:
        """How the most recent answer on this thread was produced: ``clean``, ``repaired``, ``fallback`` or ``knowledge``."""

        return getattr(self._diagnostics, "outcome", "")

    @last_outcome.setter
    def last_outcome(self, value: str) -> None:
        self._diagnostics.outcome = value

    @property
    def last_draft(self) -> str:
        """The first draft of the most recent answer on this thread."""

        return getattr(self._diagnostics, "draft", "")

    @last_draft.setter
    def last_draft(self, value: str) -> None:
        self._diagnostics.draft = value

    def diagnostics(self) -> dict[str, Any]:
        """What happened to the model's drafts on this thread's most recent call."""

        return {
            "outcome": self.last_outcome,
            "draft": self.last_draft[:4000],
            "attempts": [dict(attempt) for attempt in getattr(self._diagnostics, "attempts", [])],
            "citation_completions": list(getattr(self._diagnostics, "completions", [])),
        }

    def _keep_draft(self, text: str) -> None:
        drafts = getattr(self._diagnostics, "drafts", None)
        if drafts is None:
            drafts = self._diagnostics.drafts = []
        drafts.append(text or "")

    def _complete(self, answer: str, evidence, results) -> str:
        """Deterministic citation completion before the verifier sees a draft."""

        from v2.agent_v2.verification import complete_citations

        completed, notes = complete_citations(answer, evidence, results)
        if notes:
            existing = getattr(self._diagnostics, "completions", None)
            if existing is None:
                existing = self._diagnostics.completions = []
            existing.extend(notes)
        return completed

    def _record_attempt(self, stage: str, *, ok: bool, warnings=(), unknown_citations=(), ungrounded_numbers=()) -> None:
        attempts = getattr(self._diagnostics, "attempts", None)
        if attempts is None:
            attempts = self._diagnostics.attempts = []
        attempts.append(
            {
                "stage": stage,
                "ok": bool(ok),
                "warnings": [str(value) for value in warnings],
                "unknown_citations": [str(value) for value in unknown_citations],
                "ungrounded_numbers": [str(value) for value in ungrounded_numbers],
            }
        )

    def _record_report(self, stage: str, report) -> None:
        self._record_attempt(stage, ok=report.ok, warnings=report.warnings, unknown_citations=report.unknown_citations, ungrounded_numbers=report.ungrounded_numbers)

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
如果证据的方向与问题的前提相反（例如问为什么跌，证据显示今日上涨），先点明两者指的是不同区间或口径，再回答用户实际所指的那个区间；不要只否定前提，也不要用当日数据回答关于更长区间的问题。assumptions 中以 context_frame 开头的说明描述了用户追问所指的对象和区间，必须遵循。
不要把历史回测写成未来收益保证。不要输出未在证据中出现的数字。
不要向用户提内部工具、能力或角色名（归因者、申报阅读者、规划器、fan-out、capability 等）；直接说事实和来源类型（新闻、SEC 申报、盯盘记录、行情）。

严格遵循输入中的 response_style：
- brief：先用一句话直接回答用户问的那个量或对象（总额、盈亏、名单、日期、谁更强），这些被直接询问的数字和名字必须原样给出，不得因为篇幅省略；比较或排名问题必须点名每个候选并给出用来比较的数字；只展开被点名的对象，未被问到的个股不要逐一复述；覆盖不全时用一句话说明未覆盖的对象。然后用 3—5 个短段落、约 300—500 个中文字完成回答，挑选最有决策价值的 3—5 条事实，只讲一个主要风险和最重要的数据缺口，最后指出接下来值得观察什么。不要使用标题、表格、分隔线、编号清单、“正面/负面/中性”标签、“必须说明”或单独的免责声明章节；不要重复同一事实。
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
        self._reset_diagnostics()
        try:
            answer = self._draft(messages, results, evidence)
            self.last_draft = answer
            if plan.answer_mode == AnswerMode.GENERAL_KNOWLEDGE:
                self.last_outcome = "knowledge"
                return answer
            from v2.agent_v2.verification import verify_answer

            answer = self._complete(answer, evidence, results)
            self._keep_draft(answer)
            report = verify_answer(answer, evidence, answer_mode=plan.answer_mode, results=results)
            self._record_report("draft", report)
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
                    {"role": "user", "content": repair_instruction(report, evidence)},
                ],
                results,
                evidence,
            )
            repair = self._complete(repair, evidence, results)
            self._keep_draft(repair)
            repair_report = verify_answer(repair, evidence, answer_mode=plan.answer_mode, results=results)
            self._record_report("repair", repair_report)
            if repair_report.ok:
                self.last_outcome = "repaired"
                return repair
        except (LLMError, ValueError, TypeError) as exc:
            self._record_attempt("error", ok=False, warnings=(f"{type(exc).__name__}: {str(exc)[:200]}",))
        self._log_fallback(request)
        return self.fallback.synthesize(request, plan, results, evidence)

    def _log_fallback(self, request) -> None:
        """Every rejected draft, with what the verifier said, so a fallback can be diagnosed from the server log."""

        attempts = getattr(self._diagnostics, "attempts", []) or []
        drafts = getattr(self._diagnostics, "drafts", []) or []
        for index, attempt in enumerate(attempts):
            text = drafts[index] if index < len(drafts) else ""
            logger.warning(
                "agent_v2 synthesis fell back (%s) stage=%s warnings=%s unknown=%s ungrounded=%s draft=%r",
                (request.text or "")[:80],
                attempt.get("stage"),
                attempt.get("warnings"),
                attempt.get("unknown_citations"),
                attempt.get("ungrounded_numbers"),
                text[:3000],
            )

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
            "evidence": [item.to_dict() for item in _select_evidence(results, evidence, 40)],
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


def _select_evidence(results: list[ToolEnvelope], evidence: list[EvidenceItem], cap: int) -> list[EvidenceItem]:
    """Round-robin across results so a fan-out's later holdings still reach the model.

    Taking the first ``cap`` items in ledger order starves everything after
    the first few results; six holdings' research would show the model one
    or two of them.
    """

    if len(evidence) <= cap:
        return list(evidence)
    by_id = {item.id: item for item in evidence}
    queues = [[item.id for item in result.evidence if item.id in by_id] for result in results]
    orphans = [item.id for item in evidence if not any(item.id in queue for queue in queues)]
    if orphans:
        queues.append(orphans)
    chosen: list[str] = []
    seen: set[str] = set()
    while len(chosen) < cap and any(queues):
        for queue in queues:
            while queue:
                candidate = queue.pop(0)
                if candidate not in seen:
                    seen.add(candidate)
                    chosen.append(candidate)
                    break
            if len(chosen) >= cap:
                break
    return [by_id[value] for value in chosen]


_NEARBY_UNGROUNDED = re.compile(r"^引用未支持邻近数字：([^（]+)")


def repair_instruction(report: VerificationReport, evidence: list[EvidenceItem] | None = None) -> str:
    """Tell the model exactly what failed and what must survive the rewrite.

    An ungrounded number is usually a real figure cited with the wrong id;
    naming the items that do carry it turns the repair into swapping an id
    instead of guessing.
    """

    lines = ["校验未通过，请重写完整回答。"]
    # Figures the verifier could not ground: the answer-wide list, plus the
    # ones a sentence cited with the wrong item (those arrive as warnings).
    numbers = list(report.ungrounded_numbers[:12])
    for warning in report.warnings:
        match = _NEARBY_UNGROUNDED.match(str(warning))
        if match:
            numbers.extend(value.strip() for value in match.group(1).split(",") if value.strip())
    numbers = list(dict.fromkeys(numbers))[:12]
    if numbers:
        located = []
        missing = []
        for number in numbers:
            ids = locate_number(number, list(evidence or []))
            (located if ids else missing).append((number, ids))
        if located:
            lines.append("以下数字引用的证据不含该数字，但下列证据含有它，请改用这些 id 引用：" + "；".join(f"{number} 见 " + "、".join(f"[{value}]" for value in ids) for number, ids in located) + "。")
        if missing:
            lines.append(
                "以下数字在本轮证据中找不到：" + "、".join(number for number, _ in missing) + "。"
                "只能使用证据中出现的数字；若是你自己的计算，请把算式完整写出（例如 22.4% + 18.2% = 40.6%）；无法支持的数字直接删掉，宁可省略也不要编造。"
            )
    if report.unknown_citations:
        lines.append("以下引用 id 不存在：" + "、".join(report.unknown_citations[:12]) + "。方括号内只能原样使用 evidence 数组中真实存在的 id。")
    for warning in report.warnings[:6]:
        if _NEARBY_UNGROUNDED.match(str(warning)):
            continue  # handled above, with the ids that carry the figures
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
