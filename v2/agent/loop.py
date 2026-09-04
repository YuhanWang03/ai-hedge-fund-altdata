"""The agent loop — model-driven control flow over the existing tool surface.

Contrast with ``v2/bot/commands.py:cmd_nl``, which is the same capability set
under code-driven control flow::

    # bot: exactly one LLM call, exactly one tool call, no feedback
    intent = classify(text)          # LLM picks a label
    answer = DISPATCH[intent](args)  # code picks the function

    # agent: the model sees each result and decides what happens next
    while not done:
        response = llm(messages, tools)
        if not response.tool_calls:
            break
        results  = execute(response.tool_calls)   # in parallel
        messages += observations(results)

Everything else in this file exists because that ``while`` is unbounded and the
model is fallible. Five mechanisms fence it in, each aimed at a failure this
loop actually produces:

* **Budgets** (steps / tool calls / wall clock) with a forced final turn, so the
  run always ends with an answer instead of a truncation.
* **Errors as observations** — a raising tool feeds the model, it does not kill
  the run. This is the single biggest behavioural difference from the bot, where
  an exception reaches ``main._error_handler`` and the user reads a class name.
* **Duplicate-call suppression**, because the characteristic failure of a
  tool-calling loop is re-issuing an identical call and burning the budget.
* **Recency-weighted context compression** (see ``context.py``) — these tools
  return human-sized cards, not JSON.
* **A grounding check with one repair round** (see ``grounding.py``), which
  preserves the project's original guarantee once the model is allowed to write
  the prose.

Nothing here imports the bot at module level, and nothing under ``v2/bot`` is
modified: the loop is an alternative front-end onto the same responders.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from v2.agent import grounding
from v2.agent.context import Step, Trajectory, extract_note
from v2.agent.llm import LLMClient, LLMError, LLMResponse, ToolCall, build_llm
from v2.agent.prompts import FORCE_FINAL_SUFFIX, SYSTEM_PROMPT
from v2.agent.registry import ToolRegistry, ToolResult


@dataclass
class AgentConfig:
    """Budgets and policy for one run.

    Defaults are tuned for this tool set: responders take 1.5-8s each, so eight
    steps with parallel fan-out is roughly a 60-90s worst case — the ceiling a
    chat UI can hide behind a typing indicator.
    """

    max_steps: int = 8
    max_tool_calls: int = 20
    max_seconds: float = 150.0
    parallel: bool = True
    max_parallel: int = 6
    allow_mutations: bool = False
    grounding_repair: bool = True
    fresh_observations: int = 3
    stale_chars: int = 600
    system_prompt: str = SYSTEM_PROMPT


@dataclass
class AgentResult:
    """Everything one run produced — answer, evidence, and the cost of getting it."""

    query: str
    answer: str
    trajectory: Trajectory
    stop_reason: str
    elapsed_ms: int
    grounding: grounding.GroundingReport = field(default_factory=grounding.GroundingReport)
    repairs: int = 0
    repaired_figures: list[str] = field(default_factory=list)
    deduped_calls: int = 0
    forced_final: bool = False
    error: str = ""

    def stats(self) -> dict[str, Any]:
        merged = dict(self.trajectory.stats())
        merged.update({
            "mode": "agent",
            "elapsed_ms": self.elapsed_ms,
            "stop_reason": self.stop_reason,
            "grounding_ratio": round(self.grounding.ratio, 3),
            "ungrounded_figures": len(self.grounding.ungrounded),
            "repairs": self.repairs,
            "deduped_calls": self.deduped_calls,
            "forced_final": self.forced_final,
        })
        return merged


def _emit(event: str, **payload: Any) -> None:
    """Forward to the project's trace SDK when a trace is active; no-op otherwise.

    Reuses ``v2/observability`` rather than inventing a second event stream, so
    agent runs render in the existing dashboard. Imported lazily and defensively
    because that package pulls in the wider project.
    """
    try:
        from v2.observability import emit as _project_emit
        _project_emit(event, **payload)
    except Exception:  # noqa: BLE001 — telemetry must never break a run
        pass


def _execute_calls(
    calls: list[ToolCall],
    registry: ToolRegistry,
    trajectory: Trajectory,
    config: AgentConfig,
) -> tuple[list[ToolResult], int]:
    """Run one batch of tool calls, in parallel when there is more than one.

    Responders are blocking I/O (HTTP + SQLite), so threads are the right tool:
    five ``earnings_view`` calls cost one round-trip instead of five. The bot has
    no equivalent because it never issues more than one call per message.
    """
    seen = trajectory.previous_signatures()
    planned: list[tuple[ToolCall, ToolResult | None]] = []
    deduped = 0

    for call in calls:
        if call.parse_error:
            planned.append((call, ToolResult(
                name=call.name, args={}, ok=False,
                content=f"{call.parse_error}. Raw arguments: {call.raw_arguments[:200]}",
                error_kind="bad_json",
            )))
            continue
        signature = trajectory.call_signature(call)
        if signature in seen:
            deduped += 1
            planned.append((call, ToolResult(
                name=call.name, args=call.arguments, ok=False,
                content=("this exact call already ran in this session — its result is "
                         "above. Use it, or call a different tool."),
                error_kind="duplicate_call",
            )))
            continue
        seen.add(signature)
        planned.append((call, None))

    pending = [(index, call) for index, (call, result) in enumerate(planned) if result is None]
    results: list[ToolResult | None] = [result for _, result in planned]

    if pending:
        if config.parallel and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=min(config.max_parallel, len(pending))) as pool:
                futures = {
                    pool.submit(registry.call, call.name, call.arguments): index
                    for index, call in pending
                }
                for future, index in futures.items():
                    results[index] = future.result()
        else:
            for index, call in pending:
                results[index] = registry.call(call.name, call.arguments)

    finished = [r for r in results if r is not None]
    for result in finished:
        _emit("agent_tool_result", tool=result.name, ok=result.ok,
              elapsed_ms=result.elapsed_ms, error_kind=result.error_kind)
    return finished, deduped


def run_agent(
    query: str,
    *,
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    config: AgentConfig | None = None,
) -> AgentResult:
    """Answer ``query`` by letting the model drive the tool calls."""
    config = config or AgentConfig()
    llm = llm or build_llm()
    registry = registry or ToolRegistry(allow_mutations=config.allow_mutations)

    started = time.time()
    trajectory = Trajectory(
        query=query,
        started_ms=int(started * 1000),
        fresh_observations=config.fresh_observations,
        stale_chars=config.stale_chars,
    )
    directives: list[str] = []
    tool_schemas = registry.schemas()
    answer = ""
    stop_reason = "max_steps"
    repairs = 0
    repaired_figures: list[str] = []
    deduped_total = 0
    error = ""
    forced_final = False
    report = grounding.GroundingReport()

    _emit("agent_start", query=query[:200], tools=len(tool_schemas))

    step_index = 0
    while step_index < config.max_steps:
        elapsed = time.time() - started
        budget_spent = (
            trajectory.tool_calls >= config.max_tool_calls
            or elapsed >= config.max_seconds
            or step_index == config.max_steps - 1
        )

        messages = trajectory.to_messages(config.system_prompt)
        for directive in directives:
            messages.append({"role": "user", "content": directive})
        if budget_spent:
            messages.append({"role": "user", "content": FORCE_FINAL_SUFFIX})

        step_started = time.time()
        try:
            response = llm.complete(messages, None if budget_spent else tool_schemas)
        except LLMError as exc:
            error = str(exc)
            stop_reason = "llm_error"
            _emit("agent_error", error=error[:300])
            break

        step = Step(index=step_index, response=response,
                    elapsed_ms=int((time.time() - step_started) * 1000))
        trajectory.steps.append(step)
        step_index += 1

        # -- the model chose to answer -------------------------------------
        if not response.tool_calls:
            answer = response.text
            report = grounding.check(answer, trajectory.observations_text())
            can_repair = config.grounding_repair and repairs < 1 and not budget_spent
            if report.ok or not can_repair:
                stop_reason = "final_answer" if report.ok else "final_answer_ungrounded"
                forced_final = budget_spent
                break
            repairs += 1
            repaired_figures.extend(report.ungrounded)
            directives.append(grounding.repair_instruction(report))
            _emit("agent_grounding_repair", ungrounded=len(report.ungrounded))
            continue

        # -- the model chose to act ----------------------------------------
        if response.text:
            note = extract_note(response.text)
            if note:
                trajectory.notes.append(note)
        _emit("agent_step", step=step_index,
              tools=[c.name for c in response.tool_calls])

        results, deduped = _execute_calls(response.tool_calls, registry, trajectory, config)
        step.results = results
        deduped_total += deduped

        if budget_spent:
            stop_reason = "budget_exhausted"
            forced_final = True
            break

    if not answer and stop_reason not in ("llm_error",):
        answer = ("（未能在预算内给出结论）已收集的观测："
                  + ", ".join(trajectory.distinct_tools()))

    result = AgentResult(
        query=query,
        answer=answer,
        trajectory=trajectory,
        stop_reason=stop_reason,
        elapsed_ms=int((time.time() - started) * 1000),
        grounding=report,
        repairs=repairs,
        repaired_figures=repaired_figures,
        deduped_calls=deduped_total,
        forced_final=forced_final,
        error=error,
    )
    _emit("agent_end", **{k: v for k, v in result.stats().items()
                          if isinstance(v, (int, float, str))})
    return result
