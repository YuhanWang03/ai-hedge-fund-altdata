"""Policy-gated capability registry and deterministic DAG executor."""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from v2.agent_v2.catalog import CapabilityCatalog
from v2.agent_v2.evidence import EvidenceLedger
from v2.agent_v2.models import (
    BudgetClass,
    ExecutionPlan,
    NormalizedRequest,
    PlanTask,
    ProgressEvent,
    ResultStatus,
    RunStatus,
    ToolEnvelope,
)
from v2.agent_v2.ports import CapabilityHandler, ProgressSink

_TASK_LIMITS = {
    BudgetClass.DIRECT: 1,
    BudgetClass.FOCUSED: 2,
    BudgetClass.STANDARD: 5,
    BudgetClass.COMPARISON: 5,
    BudgetClass.PORTFOLIO: 7,
    BudgetClass.LAB: 2,
    BudgetClass.DEEP: 12,
}

# Wall-clock allowance for the whole capability plan.  A capability that is
# still running when the deadline passes is reported as failed and the tasks
# that have not started are skipped; the run always ends with an answer.
_TIME_LIMITS_SECONDS = {
    BudgetClass.DIRECT: 30.0,
    BudgetClass.FOCUSED: 60.0,
    BudgetClass.STANDARD: 180.0,
    BudgetClass.COMPARISON: 240.0,
    BudgetClass.PORTFOLIO: 240.0,
    BudgetClass.LAB: 600.0,
    BudgetClass.DEEP: 1800.0,
}


def task_limit(budget: BudgetClass) -> int:
    """Maximum number of tasks the executor accepts for one budget class."""
    return _TASK_LIMITS[budget]


def time_limit(budget: BudgetClass) -> float:
    """Wall-clock seconds the executor grants one budget class."""
    return _TIME_LIMITS_SECONDS[budget]


class PlanValidationError(ValueError):
    """The planned task graph cannot be executed safely."""


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    request: NormalizedRequest
    budget: BudgetClass
    allow_mutations: bool = False
    allow_web: bool = False
    on_progress: ProgressSink | None = None
    #: Absolute ``time.monotonic()`` deadline; ``None`` means the budget default.
    deadline: float | None = None

    def remaining_seconds(self) -> float:
        limit = self.deadline if self.deadline is not None else time.monotonic() + time_limit(self.budget)
        return limit - time.monotonic()

    def emit(self, message: str, *, task_id: str = "", capability: str = "") -> None:
        if not self.on_progress:
            return
        try:
            self.on_progress(ProgressEvent(self.run_id, RunStatus.EXECUTING, message, task_id, capability))
        except Exception:  # noqa: BLE001 — progress reporting never breaks a run
            pass


@dataclass
class ExecutionOutcome:
    results: list[ToolEnvelope] = field(default_factory=list)
    ledger: EvidenceLedger = field(default_factory=EvidenceLedger)
    #: ``completed`` or ``deadline``; the orchestrator surfaces it on the result.
    stop_reason: str = "completed"
    elapsed_ms: int = 0


class CapabilityRegistry:
    def __init__(self, catalog: CapabilityCatalog) -> None:
        self.catalog = catalog
        self._handlers: dict[str, CapabilityHandler] = {}

    def register(self, name: str, handler: CapabilityHandler) -> None:
        if self.catalog.get(name) is None:
            raise KeyError(f"capability is not declared in the catalog: {name}")
        self._handlers[name] = handler

    def registered(self, name: str) -> bool:
        return name in self._handlers

    def execute(self, task: PlanTask, context: ExecutionContext) -> ToolEnvelope:
        spec = self.catalog.get(task.capability)
        if spec is None:
            return ToolEnvelope(task.capability, ResultStatus.FAILED, errors=["unknown capability"])
        schema = spec.input_schema
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in task.arguments]
        extra = [name for name in task.arguments if name not in properties]
        if missing or extra:
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if extra:
                details.append("unknown: " + ", ".join(extra))
            return ToolEnvelope(task.capability, ResultStatus.FAILED, errors=["invalid arguments (" + "; ".join(details) + ")"])
        if spec.mutating and not context.allow_mutations:
            return ToolEnvelope(
                task.capability,
                ResultStatus.FAILED,
                errors=["mutation requires explicit confirmation"],
            )
        if spec.pack == "web" and not context.allow_web:
            return ToolEnvelope(task.capability, ResultStatus.FAILED, errors=["web fallback is disabled"])
        handler = self._handlers.get(task.capability)
        if handler is None:
            return ToolEnvelope(
                task.capability,
                ResultStatus.FAILED,
                errors=["capability adapter is not registered"],
            )
        started = time.time()
        try:
            result = handler(dict(task.arguments), context)
            result.elapsed_ms = result.elapsed_ms or int((time.time() - started) * 1000)
            return result
        except Exception as exc:  # a capability failure is data for the orchestrator
            return ToolEnvelope(
                task.capability,
                ResultStatus.FAILED,
                errors=[f"{type(exc).__name__}: {str(exc)[:300]}"],
                elapsed_ms=int((time.time() - started) * 1000),
            )


class ExecutionEngine:
    def __init__(self, registry: CapabilityRegistry, *, max_parallel: int = 4) -> None:
        self.registry = registry
        self.max_parallel = max(1, max_parallel)

    def validate(self, plan: ExecutionPlan) -> None:
        if len(plan.tasks) > _TASK_LIMITS[plan.budget]:
            raise PlanValidationError(f"plan has {len(plan.tasks)} tasks; {plan.budget.value} budget allows {_TASK_LIMITS[plan.budget]}")
        ids = [task.id for task in plan.tasks]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("plan task ids must be unique")
        known = set(ids)
        if any(set(task.depends_on) - known for task in plan.tasks):
            raise PlanValidationError("plan contains an unknown dependency")

    def run(self, plan: ExecutionPlan, context: ExecutionContext) -> ExecutionOutcome:
        self.validate(plan)
        started = time.monotonic()
        deadline = context.deadline if context.deadline is not None else started + time_limit(context.budget)

        pending = {task.id: task for task in plan.tasks}
        completed: dict[str, ToolEnvelope] = {}
        outcome = ExecutionOutcome()

        def finish(task: PlanTask, result: ToolEnvelope) -> None:
            completed[task.id] = result
            outcome.results.append(result)
            pending.pop(task.id, None)
            outcome.ledger.ingest(result)

        while pending:
            ready = [task for task in pending.values() if all(dependency in completed for dependency in task.depends_on)]
            if not ready:
                raise PlanValidationError("plan dependency cycle detected")

            runnable: list[PlanTask] = []
            for task in ready:
                failed_dependency = any(not completed[dep].ok for dep in task.depends_on)
                if failed_dependency and task.required:
                    finish(task, ToolEnvelope(task.capability, ResultStatus.SKIPPED, errors=["required dependency failed"]))
                else:
                    runnable.append(task)
            if not runnable:
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                for task in list(pending.values()):
                    finish(task, ToolEnvelope(task.capability, ResultStatus.SKIPPED, errors=["wall-clock budget exhausted before the task started"]))
                outcome.stop_reason = "deadline"
                break

            for task in runnable:
                context.emit(task.purpose or f"running {task.capability}", task_id=task.id, capability=task.capability)
            pool = ThreadPoolExecutor(max_workers=min(self.max_parallel, len(runnable)))
            futures: dict[Future[ToolEnvelope], PlanTask] = {pool.submit(self.registry.execute, task, context): task for task in runnable}
            timed_out = False
            try:
                outstanding = set(futures)
                while outstanding:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    done, outstanding = wait(outstanding, timeout=remaining, return_when=FIRST_COMPLETED)
                    for future in done:
                        finish(futures[future], future.result())
                if timed_out:
                    for future in outstanding:
                        task = futures[future]
                        finish(task, ToolEnvelope(task.capability, ResultStatus.FAILED, errors=[f"timed out after {time_limit(context.budget):.0f}s wall-clock budget"]))
            finally:
                # Threads that overran the deadline cannot be killed; they are
                # left to finish in the background without blocking the answer.
                pool.shutdown(wait=False, cancel_futures=True)
            if timed_out:
                for task in list(pending.values()):
                    finish(task, ToolEnvelope(task.capability, ResultStatus.SKIPPED, errors=["wall-clock budget exhausted"]))
                outcome.stop_reason = "deadline"
                break

        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome
