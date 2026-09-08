"""Policy-gated capability registry and deterministic DAG executor."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

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


def task_limit(budget: BudgetClass) -> int:
    """Maximum number of tasks the executor accepts for one budget class."""
    return _TASK_LIMITS[budget]


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

    def emit(self, message: str, *, task_id: str = "", capability: str = "") -> None:
        if self.on_progress:
            self.on_progress(ProgressEvent(self.run_id, RunStatus.EXECUTING, message, task_id, capability))


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

    def run(
        self,
        plan: ExecutionPlan,
        context: ExecutionContext,
    ) -> tuple[list[ToolEnvelope], EvidenceLedger]:
        if len(plan.tasks) > _TASK_LIMITS[plan.budget]:
            raise PlanValidationError(f"plan has {len(plan.tasks)} tasks; {plan.budget.value} budget allows " f"{_TASK_LIMITS[plan.budget]}")
        ids = [task.id for task in plan.tasks]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("plan task ids must be unique")
        known = set(ids)
        if any(set(task.depends_on) - known for task in plan.tasks):
            raise PlanValidationError("plan contains an unknown dependency")

        pending = {task.id: task for task in plan.tasks}
        completed: dict[str, ToolEnvelope] = {}
        ordered: list[ToolEnvelope] = []
        ledger = EvidenceLedger()

        while pending:
            ready = [task for task in pending.values() if all(dependency in completed for dependency in task.depends_on)]
            if not ready:
                raise PlanValidationError("plan dependency cycle detected")

            runnable: list[PlanTask] = []
            for task in ready:
                failed_dependency = any(not completed[dep].ok for dep in task.depends_on)
                if failed_dependency and task.required:
                    result = ToolEnvelope(
                        task.capability,
                        ResultStatus.SKIPPED,
                        errors=["required dependency failed"],
                    )
                    completed[task.id] = result
                    ordered.append(result)
                    pending.pop(task.id)
                else:
                    runnable.append(task)

            if not runnable:
                continue
            for task in runnable:
                context.emit(
                    task.purpose or f"running {task.capability}",
                    task_id=task.id,
                    capability=task.capability,
                )
            with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(runnable))) as pool:
                futures = [(task, pool.submit(self.registry.execute, task, context)) for task in runnable]
                for task, future in futures:
                    result = future.result()
                    completed[task.id] = result
                    ordered.append(result)
                    pending.pop(task.id)
                    ledger.ingest(result)
        return ordered, ledger
