"""Top-level Agent V2 state machine."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, replace

from v2.agent_v2.catalog import CapabilityCatalog, default_catalog
from v2.agent_v2.evidence import EvidenceConflictError
from v2.agent_v2.execution import (
    CapabilityRegistry,
    ExecutionContext,
    ExecutionEngine,
    ExecutionOutcome,
    PlanValidationError,
    time_limit,
)
from v2.agent_v2.models import (
    AgentResult,
    AnswerMode,
    ExecutionPlan,
    NormalizedRequest,
    PendingMutation,
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

#: Seconds the one web attempt gets when the internal step spent the budget.
WEB_FALLBACK_GRACE_SECONDS = 45.0

_CONFIRM = re.compile(r"^\s*(?:确认|确定|是的?|好的?|执行|yes|y|ok|confirm)\s*[。！!.]?\s*$", re.I)
_CANCEL = re.compile(r"^\s*(?:取消|不用了?|不要|算了|否|no|n|cancel)\s*[。！!.]?\s*$", re.I)


@dataclass(frozen=True)
class AgentV2Config:
    max_parallel: int = 4
    enable_web_fallback: bool = False
    #: Bypass the confirmation step; only for tests and trusted automation.
    allow_mutations: bool = False
    #: Override the per-budget wall-clock allowance (seconds) for every run.
    max_seconds: float | None = None


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
        if not sink:
            return
        try:
            sink(ProgressEvent(run_id, status, message))
        except Exception:  # noqa: BLE001 — progress reporting never breaks a run
            pass

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

        # A pending write is resolved before anything else: "确认" executes it,
        # "取消" drops it, and any other message drops it and proceeds normally.
        pending = self.session.pop_pending(session_id) if self.session is not None and session_id else None
        if pending is not None:
            if _CONFIRM.match(text or ""):
                return self._execute_confirmed(run_id, pending, session_id, on_progress, started)
            if _CANCEL.match(text or ""):
                request = normalize_request(text, session_id=session_id)
                decision = RouteDecision(RouteKind.COMMAND, ("command",), "pending mutation cancelled")
                return self._result(run_id, request, decision, pending, RunStatus.CANCELLED, "已取消，未执行任何修改。", AnswerMode.TOOL_GROUNDED, started)

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
                if resolution.frame:
                    resolution_metadata["context_frame"] = dict(resolution.frame)
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

        if plan.requires_confirmation and not self.config.allow_mutations:
            return self._await_confirmation(run_id, request, decision, plan, started)
        if plan.direct_answer and not plan.tasks:
            # A capability overview is a complete answer; a clarification request is not.
            complete = plan.answer_mode == AnswerMode.GENERAL_KNOWLEDGE
            return self._result(run_id, request, decision, plan, RunStatus.COMPLETED if complete else RunStatus.PARTIAL, plan.direct_answer, AnswerMode.GENERAL_KNOWLEDGE if complete else AnswerMode.INSUFFICIENT_EVIDENCE, started)

        context = self._context(run_id, request, plan, on_progress, started)
        return self._execute(run_id, request, decision, plan, context, on_progress, started)

    # -- confirmation ---------------------------------------------------------

    @staticmethod
    def _pending_mutation(plan: ExecutionPlan) -> PendingMutation | None:
        task = next((task for task in plan.tasks if task.capability == "state.mutate"), None)
        if task is None:
            return None
        return PendingMutation(
            operation=str(task.arguments.get("operation") or ""),
            payload=dict(task.arguments.get("payload") or {}),
            description=task.purpose or task.capability,
        )

    def _await_confirmation(self, run_id, request, decision, plan, started) -> AgentResult:
        mutation = self._pending_mutation(plan)
        if mutation is None:
            return self._result(run_id, request, decision, plan, RunStatus.PARTIAL, self.synthesizer.synthesize(request, plan, [], []), AnswerMode.INSUFFICIENT_EVIDENCE, started)
        if self.session is not None and request.session_id:
            self.session.set_pending(request.session_id, plan)
            answer = f"将执行写操作：{mutation.description}。回复「确认」执行，回复「取消」放弃；当前没有执行任何修改。"
        else:
            answer = f"将执行写操作：{mutation.description}。该渠道没有会话，无法接收确认；当前没有执行任何修改。"
        return self._result(run_id, request, decision, plan, RunStatus.WAITING_CONFIRMATION, answer, AnswerMode.TOOL_GROUNDED, started, pending_mutation=mutation)

    def _execute_confirmed(self, run_id, plan: ExecutionPlan, session_id: str, on_progress, started) -> AgentResult:
        request = normalize_request(plan.objective, session_id=session_id, metadata={"confirmed_mutation": True})
        decision = RouteDecision(RouteKind.COMMAND, ("command",), "user confirmed a pending mutation")
        context = self._context(run_id, request, plan, on_progress, started, allow_mutations=True)
        return self._execute(run_id, request, decision, plan, context, on_progress, started)

    # -- execution ------------------------------------------------------------

    def _context(self, run_id, request, plan, on_progress, started, *, allow_mutations: bool | None = None) -> ExecutionContext:
        limit = self.config.max_seconds if self.config.max_seconds is not None else time_limit(plan.budget)
        elapsed = time.time() - started
        return ExecutionContext(
            run_id=run_id,
            request=request,
            budget=plan.budget,
            allow_mutations=self.config.allow_mutations if allow_mutations is None else allow_mutations,
            allow_web=request.allow_web,
            on_progress=on_progress,
            deadline=time.monotonic() + max(0.0, limit - elapsed),
        )

    def _execute(self, run_id, request, decision, plan, context: ExecutionContext, on_progress, started) -> AgentResult:
        self._emit(on_progress, run_id, RunStatus.EXECUTING, "executing capability plan")
        try:
            outcome = self.executor.run(plan, context)
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

        plan, outcome = self._web_fallback(request, decision, plan, outcome, context)
        results = outcome.results

        self._emit(on_progress, run_id, RunStatus.SYNTHESIZING, "synthesizing evidence")
        evidence = outcome.ledger.items()
        answer = self.synthesizer.synthesize(request, plan, results, evidence)
        synthesis = self._synthesis_diagnostics()
        answer_mode = plan.answer_mode
        if not plan.tasks and decision.kind != RouteKind.GENERAL_KNOWLEDGE:
            answer_mode = AnswerMode.INSUFFICIENT_EVIDENCE
        self._emit(on_progress, run_id, RunStatus.VERIFYING, "verifying citations")
        verification = verify_answer(answer, evidence, answer_mode=answer_mode, results=results)
        failures = [result for result in results if not result.ok]
        knowledge_unavailable = not results and decision.kind == RouteKind.GENERAL_KNOWLEDGE and not bool(getattr(self.synthesizer, "supports_general_knowledge", False))
        if failures or not verification.ok or knowledge_unavailable or outcome.stop_reason != "completed" or (not results and decision.kind != RouteKind.GENERAL_KNOWLEDGE):
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
            stop_reason=outcome.stop_reason,
            synthesis=synthesis,
        )

    def _synthesis_diagnostics(self) -> dict:
        diagnostics = getattr(self.synthesizer, "diagnostics", None)
        if not callable(diagnostics):
            return {}
        try:
            return dict(diagnostics())
        except Exception:  # noqa: BLE001 — diagnostics never break an answer
            return {}

    def _web_fallback(self, request, decision, plan, outcome: ExecutionOutcome, context: ExecutionContext):
        """Make one bounded web attempt only after internal evidence is absent or failed."""

        eligible = plan.web_fallback_allowed and request.allow_web and decision.kind in {RouteKind.FAST_LOOKUP, RouteKind.RESEARCH} and self.registry.registered("web.research")
        existing_evidence = outcome.ledger.items()
        internal_failed = any(not result.ok for result in outcome.results)
        if not eligible or (existing_evidence and not internal_failed):
            return plan, outcome
        notes = ["Web fallback ran because internal evidence was missing or failed."]
        if context.remaining_seconds() < WEB_FALLBACK_GRACE_SECONDS:
            # The internal step used the budget (a timeout, typically); the
            # one web attempt gets its own grace rather than nothing, so the
            # run still ends with an answer instead of "未完成".
            object.__setattr__(context, "deadline", time.monotonic() + WEB_FALLBACK_GRACE_SECONDS)
            notes.append(f"Web fallback ran with a {WEB_FALLBACK_GRACE_SECONDS:.0f} s grace after internal capabilities used the budget.")
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
        outcome.results.append(result)
        outcome.ledger.ingest(result)
        mode = AnswerMode.MIXED if existing_evidence else AnswerMode.WEB_GROUNDED
        plan = replace(
            plan,
            tasks=(*plan.tasks, task),
            answer_mode=mode,
            assumptions=(*plan.assumptions, *notes),
        )
        return plan, outcome

    def _result(
        self,
        run_id,
        request: NormalizedRequest,
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
        stop_reason: str = "",
        pending_mutation: PendingMutation | None = None,
        synthesis: dict | None = None,
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
            stop_reason=stop_reason,
            pending_mutation=pending_mutation,
            synthesis=dict(synthesis or {}),
        )
        if self.session is not None:
            self.session.record(result)
        return result
