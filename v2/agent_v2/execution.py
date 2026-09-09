"""Policy-gated capability registry and deterministic DAG executor."""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from v2.agent_v2.catalog import CapabilityCatalog
from v2.agent_v2.evidence import EvidenceLedger
from v2.agent_v2.models import (
    BudgetClass,
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    PlanTask,
    ProgressEvent,
    ResultStatus,
    RunStatus,
    ToolEnvelope,
)
from v2.agent_v2.ports import CapabilityHandler, ProgressSink

#: Upper bound on children one fan-out task may spawn, whatever the source lists.
FAN_OUT_MAX = 8

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
        for task in plan.tasks:
            if task.fan_out is None:
                continue
            source = str(task.fan_out.get("from") or "")
            if source not in known or source == task.id:
                raise PlanValidationError(f"fan-out task {task.id} names an unknown source")
            if not task.fan_out.get("argument"):
                raise PlanValidationError(f"fan-out task {task.id} has no target argument")
            rank = task.fan_out.get("rank")
            if rank is not None and (not isinstance(rank, dict) or not rank.get("field") or not rank.get("key")):
                raise PlanValidationError(f"fan-out task {task.id} has an invalid rank spec")

    @staticmethod
    def _ordered_values(spec: dict[str, Any], source: ToolEnvelope | None) -> list[Any]:
        """Source values in fan-out order: ranked by a row field when the plan asks for it."""

        field_name = str(spec.get("field") or "tickers")
        values = list((source.metadata.get(field_name) if source is not None else None) or [])
        rank = spec.get("rank")
        if not isinstance(rank, dict) or source is None:
            return values
        rows = source.metadata.get(str(rank.get("field") or ""))
        key = str(rank.get("key") or "")
        argument = str(spec.get("argument") or "")
        if not isinstance(rows, list) or not key:
            return values
        ranked: list[tuple[float, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(argument, row.get("ticker"))
            number = row.get(key)
            if value is None or not isinstance(number, (int, float)):
                continue
            ranked.append((float(number), value))
        if not ranked:
            return values
        ranked.sort(key=lambda pair: pair[0], reverse=bool(rank.get("descending")))
        ordered = [value for _, value in ranked]
        # Values the table did not cover keep their original place after the ranked ones.
        return list(dict.fromkeys([*ordered, *values]))

    @classmethod
    def _expand(cls, task: PlanTask, completed: dict[str, ToolEnvelope]) -> tuple[list[PlanTask], ToolEnvelope | None]:
        """Replace a fan-out template with one child per source value.

        Returns the children and, when the source listed more values than the
        cap allows, a coverage note the answer must disclose.
        """

        spec = task.fan_out or {}
        source = completed.get(str(spec.get("from") or ""))
        values = cls._ordered_values(spec, source)
        limit = max(1, min(int(spec.get("max") or FAN_OUT_MAX), FAN_OUT_MAX))
        note: ToolEnvelope | None = None
        if len(values) > limit:
            chosen = ", ".join(str(value) for value in values[:limit])
            rest = ", ".join(str(value) for value in values[limit:])
            ordering = "按相关性排序后" if isinstance(spec.get("rank"), dict) else "按列表顺序"
            claim = f"{task.capability} 只覆盖了 {len(values)} 个对象中的 {limit} 个（{ordering}）：{chosen}；未覆盖：{rest}。"
            note = ToolEnvelope(
                task.capability,
                ResultStatus.PARTIAL_DATA,
                subject=task.id,
                summary="",
                evidence=[
                    EvidenceItem(
                        id=f"fan-out-coverage-{task.id}",
                        entity=task.id,
                        claim=claim,
                        source_id="execution_engine",
                        source_title="Fan-out coverage",
                        metadata={"citation_kind": "limitations", "verified": True, "covered": list(values[:limit]), "uncovered": list(values[limit:])},
                    )
                ],
                limitations=[claim],
                metadata={"fan_out_coverage": {"covered": list(values[:limit]), "uncovered": list(values[limit:])}},
            )
        children: list[PlanTask] = []
        for value in values[:limit]:
            children.append(
                PlanTask(
                    id=f"{task.id}[{value}]",
                    capability=task.capability,
                    arguments={**task.arguments, str(spec["argument"]): value},
                    depends_on=task.depends_on,
                    required=task.required,
                    purpose=task.purpose,
                )
            )
        return children, note

    def run(self, plan: ExecutionPlan, context: ExecutionContext) -> ExecutionOutcome:
        self.validate(plan)
        started = time.monotonic()
        deadline = context.deadline if context.deadline is not None else started + time_limit(context.budget)

        pending = {task.id: task for task in plan.tasks}
        completed: dict[str, ToolEnvelope] = {}
        #: fan-out template id -> child ids, so dependants of a template wait for every child.
        expanded: dict[str, list[str]] = {}
        outcome = ExecutionOutcome()

        def finish(task: PlanTask, result: ToolEnvelope) -> None:
            completed[task.id] = result
            outcome.results.append(result)
            pending.pop(task.id, None)
            outcome.ledger.ingest(result)

        def satisfied(dependency: str) -> bool:
            if dependency in expanded:
                return all(child in completed for child in expanded[dependency])
            return dependency in completed

        def dependency_ok(dependency: str) -> bool:
            if dependency in expanded:
                return all(completed[child].ok for child in expanded[dependency])
            return completed[dependency].ok

        while pending:
            ready = [task for task in pending.values() if all(satisfied(dependency) for dependency in task.depends_on)]
            if not ready:
                raise PlanValidationError("plan dependency cycle detected")

            runnable: list[PlanTask] = []
            for task in ready:
                failed_dependency = any(not dependency_ok(dep) for dep in task.depends_on)
                if failed_dependency and task.required:
                    finish(task, ToolEnvelope(task.capability, ResultStatus.SKIPPED, errors=["required dependency failed"]))
                    continue
                if task.fan_out is not None:
                    children, note = self._expand(task, completed)
                    pending.pop(task.id, None)
                    if note is not None:
                        # The truncation is a limitation of this run's evidence,
                        # so it travels with the results and is citable.
                        outcome.results.append(note)
                        outcome.ledger.ingest(note)
                    if not children:
                        expanded[task.id] = []
                        result = ToolEnvelope(task.capability, ResultStatus.SKIPPED, errors=["fan-out source listed no values"])
                        completed[task.id] = result
                        outcome.results.append(result)
                        continue
                    expanded[task.id] = [child.id for child in children]
                    for child in children:
                        pending[child.id] = child
                    runnable.extend(children)
                    continue
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
