"""Top-level Agent V2 state machine."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace

from v2.agent_v2.catalog import CapabilityCatalog, default_catalog
from v2.agent_v2.evidence import EvidenceConflictError
from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext, ExecutionEngine, PlanValidationError
from v2.agent_v2.models import (
    AgentResult,
    AnswerMode,
    ExecutionPlan,
    PlanTask,
    ProgressEvent,
    RouteDecision,
    RouteKind,
    RunStatus,
    VerificationReport,
)
from v2.agent_v2.planning import RulePlanner
from v2.agent_v2.ports import PlannerPort, ProgressSink, SessionPort, SynthesizerPort
from v2.agent_v2.routing import normalize_request, route
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer
from v2.agent_v2.verification import verify_answer


@dataclass(frozen=True)
class AgentV2Config:
    max_parallel: int = 4
    enable_web_fallback: bool = False
    execute_async_inline: bool = False
    allow_mutations: bool = False


class AgentV2:
    def __init__(
        self,
        *,
        catalog: CapabilityCatalog | None = None,
        registry: CapabilityRegistry | None = None,
        planner: PlannerPort | None = None,
        synthesizer: SynthesizerPort | None = None,
        session: SessionPort | None = None,
        config: AgentV2Config | None = None,
    ) -> None:
        self.catalog = catalog or default_catalog()
        self.registry = registry or CapabilityRegistry(self.catalog)
        self.planner = planner or RulePlanner()
        self.synthesizer = synthesizer or EvidenceSummarySynthesizer()
        self.session = session
        self.config = config or AgentV2Config()
        self.executor = ExecutionEngine(self.registry, max_parallel=self.config.max_parallel)

    @staticmethod
    def _emit(sink: ProgressSink | None, run_id: str, status: RunStatus, message: str) -> None:
        if sink:
            sink(ProgressEvent(run_id, status, message))

    def run(
        self,
        text: str,
        *,
        session_id: str = "",
        allow_web: bool = False,
        on_progress: ProgressSink | None = None,
    ) -> AgentResult:
        started = time.time()
        run_id = f"agent-v2-{uuid.uuid4().hex[:12]}"
        resolved_text = text
        resolution_metadata = {}
        if self.session is not None and session_id:
            resolution = self.session.resolve(session_id, text)
            resolved_text = resolution.text
            if resolution.rewritten:
                resolution_metadata = {
                    "rewritten": True,
                    "antecedent": resolution.antecedent,
                    "resolution_note": resolution.note,
                }
        request = normalize_request(
            resolved_text,
            session_id=session_id,
            allow_web=allow_web and self.config.enable_web_fallback,
            metadata=resolution_metadata,
        )
        decision = route(request)
        self._emit(on_progress, run_id, RunStatus.ROUTED, decision.reason)
        plan = self.planner.plan(request, decision)
        self._emit(on_progress, run_id, RunStatus.PLANNED, f"planned {len(plan.tasks)} task(s)")

        if plan.requires_confirmation:
            return self._result(
                run_id,
                request,
                decision,
                plan,
                RunStatus.WAITING_CONFIRMATION,
                "这是一个写操作。请确认准确的操作对象和参数；当前没有执行任何修改。",
                AnswerMode.TOOL_GROUNDED,
                started,
            )
        if decision.asynchronous and not self.config.execute_async_inline:
            return self._result(
                run_id,
                request,
                decision,
                plan,
                RunStatus.QUEUED,
                "该请求属于长时间任务，已生成执行计划；需要由 Web/Telegram 的任务队列接口提交。",
                plan.answer_mode,
                started,
            )

        context = ExecutionContext(
            run_id=run_id,
            request=request,
            budget=plan.budget,
            allow_mutations=self.config.allow_mutations,
            allow_web=request.allow_web,
            on_progress=on_progress,
        )
        self._emit(on_progress, run_id, RunStatus.EXECUTING, "executing capability plan")
        try:
            results, ledger = self.executor.run(plan, context)
        except Exception as exc:
            if isinstance(exc, EvidenceConflictError):
                answer = "研究结果包含冲突的证据标识，任务已安全停止。"
                warning = "证据完整性检查失败"
            elif isinstance(exc, PlanValidationError):
                answer = "执行计划无效，任务没有完成。"
                warning = "执行计划校验失败"
            else:
                answer = "工具执行发生未预期错误，任务没有完成。"
                warning = "工具执行失败"
            return self._result(
                run_id,
                request,
                decision,
                plan,
                RunStatus.FAILED,
                answer,
                AnswerMode.INSUFFICIENT_EVIDENCE,
                started,
                error=f"{type(exc).__name__}: {exc}",
                verification=VerificationReport(ok=False, warnings=(warning,)),
            )

        plan, results = self._web_fallback(request, decision, plan, results, ledger, context)

        self._emit(on_progress, run_id, RunStatus.SYNTHESIZING, "synthesizing evidence")
        evidence = ledger.items()
        answer = self.synthesizer.synthesize(request, plan, results, evidence)
        answer_mode = plan.answer_mode
        if not plan.tasks and decision.kind != RouteKind.GENERAL_KNOWLEDGE:
            answer_mode = AnswerMode.INSUFFICIENT_EVIDENCE
        self._emit(on_progress, run_id, RunStatus.VERIFYING, "verifying citations")
        verification = verify_answer(answer, evidence, answer_mode=answer_mode)
        failures = [result for result in results if not result.ok]
        knowledge_unavailable = not results and decision.kind == RouteKind.GENERAL_KNOWLEDGE and not bool(getattr(self.synthesizer, "supports_general_knowledge", False))
        if failures or not verification.ok or knowledge_unavailable or (not results and decision.kind != RouteKind.GENERAL_KNOWLEDGE):
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.COMPLETED
        return self._result(
            run_id,
            request,
            decision,
            plan,
            status,
            answer,
            answer_mode,
            started,
            results=results,
            evidence=evidence,
            verification=verification,
        )

    def _web_fallback(self, request, decision, plan, results, ledger, context):
        """Make one bounded web attempt only after internal evidence is absent or failed."""

        eligible = plan.web_fallback_allowed and request.allow_web and decision.kind in {RouteKind.FAST_LOOKUP, RouteKind.RESEARCH} and self.registry.registered("web.research")
        existing_evidence = ledger.items()
        internal_failed = any(not result.ok for result in results)
        if not eligible or (existing_evidence and not internal_failed):
            return plan, results
        topic = "company_event" if request.entities else "financial_research"
        task = PlanTask(
            id="web-fallback",
            capability="web.research",
            arguments={
                "query": request.text[:500],
                "topic": topic,
                "ticker": request.entities[0] if request.entities else "",
                "recency_days": 30,
            },
            required=False,
            purpose="fill an evidence gap left by internal capabilities",
        )
        context.emit(task.purpose, task_id=task.id, capability=task.capability)
        result = self.registry.execute(task, context)
        results = [*results, result]
        ledger.ingest(result)
        mode = AnswerMode.MIXED if existing_evidence else AnswerMode.WEB_GROUNDED
        plan = replace(
            plan,
            tasks=(*plan.tasks, task),
            answer_mode=mode,
            assumptions=(*plan.assumptions, "Web fallback ran because internal evidence was missing or failed."),
        )
        return plan, results

    def _result(
        self,
        run_id,
        request,
        decision: RouteDecision,
        plan: ExecutionPlan,
        status: RunStatus,
        answer: str,
        answer_mode: AnswerMode,
        started: float,
        *,
        results=None,
        evidence=None,
        verification=None,
        error: str = "",
    ) -> AgentResult:
        result = AgentResult(
            run_id=run_id,
            request=request,
            route=decision,
            plan=plan,
            status=status,
            answer=answer,
            answer_mode=answer_mode,
            results=list(results or []),
            evidence=list(evidence or []),
            verification=verification or VerificationReport(),
            elapsed_ms=int((time.time() - started) * 1000),
            error=error,
        )
        if self.session is not None:
            self.session.record(result)
        return result
