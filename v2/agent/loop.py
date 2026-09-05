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
from typing import Any, Callable, Literal

from v2.agent import attribution, grounding, presentation
from v2.agent.context import Step, Trajectory, extract_note
from v2.agent.llm import LLMClient, LLMError, LLMResponse, ToolCall, build_llm
from v2.agent.prompts import FORCE_FINAL_SUFFIX, SYSTEM_PROMPT
from v2.agent.registry import ToolRegistry, ToolResult


@dataclass
class StepEvent:
    """Progress signal emitted as the loop runs.

    A multi-step run takes ~11s against these tools. A chat UI that shows a
    frozen "thinking…" for that long reads as broken, and — more useful — the
    step-by-step trail is what lets a user see *where* a wrong answer went wrong.
    Delivered synchronously from the loop thread; the callback must not block.
    """

    phase: Literal["plan", "tools", "observation", "repair", "final"]
    step: int
    message: str
    tools: tuple[str, ...] = ()
    ok: bool = True


def _notify(on_step: "Callable[[StepEvent], None] | None", event: StepEvent) -> None:
    """Progress reporting is never allowed to break a run."""
    if on_step is None:
        return
    try:
        on_step(event)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class AgentConfig:
    """Budgets and policy for one run.

    Defaults are tuned for this tool set: responders take 1.5-8s each, so eight
    steps with parallel fan-out is roughly a 60-90s worst case — the ceiling a
    chat UI can hide behind a typing indicator.
    """

    max_steps: int = 8
    max_tool_calls: int = 20
    #: How many times one tool may be called in a run. The largest collection
    #: these tools fan out over is the 8-position portfolio, so eight lets every
    #: legitimate per-holding sweep through and stops the runs that kept going:
    #: r07 spent 25 calls on a three-name watchlist.
    #:
    #: A hard limit rather than a hint, on purpose. Two experiments (rounds 10
    #: and 11) established that a description can change *which* tool is picked
    #: and never *whether* one more is called — the loop has no "enough" signal
    #: for the model to respond to, so the stop has to be imposed.
    max_calls_per_tool: int = 8
    max_seconds: float = 150.0
    parallel: bool = True
    max_parallel: int = 6
    allow_mutations: bool = False
    grounding_repair: bool = True
    #: Also verify that figures are attributed to the entity they belong to.
    #: Grounding alone cannot see this: the numbers are real, the subject is not.
    attribution_check: bool = True
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
    attribution: attribution.AttributionReport = field(
        default_factory=attribution.AttributionReport)
    repairs: int = 0
    repaired_figures: list[str] = field(default_factory=list)
    #: The answer the checks rejected, when a repair round ran. Kept because a
    #: rewrite can "pass" by deleting the facts the draft got right — and
    #: without the draft that reads as the model failing, not the check.
    draft: str = ""
    #: What rejected it: ungrounded figures and misattribution findings.
    draft_findings: list[str] = field(default_factory=list)
    deduped_calls: int = 0
    capped_calls: int = 0
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
            "misattributed": len(self.attribution.misattributed)
                             + len(self.attribution.empty_presented),
            "repairs": self.repairs,
            "deduped_calls": self.deduped_calls,
            "capped_calls": self.capped_calls,
            "calls_by_tool": self.trajectory.calls_by_tool(),
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
    used = trajectory.calls_by_tool()
    planned: list[tuple[ToolCall, ToolResult | None]] = []
    deduped = 0
    capped = 0

    for call in calls:
        if call.parse_error:
            planned.append((call, ToolResult(
                name=call.name, args={}, ok=False,
                content=f"{call.parse_error}. Raw arguments: {call.raw_arguments[:200]}",
                error_kind="bad_json",
            )))
            continue
        if used.get(call.name, 0) >= config.max_calls_per_tool:
            capped += 1
            planned.append((call, ToolResult(
                name=call.name, args=call.arguments, ok=False,
                content=(f"'{call.name}' has already run "
                         f"{config.max_calls_per_tool} times in this session, "
                         "which is the per-tool limit. Answer from what those "
                         "calls returned, or use a different tool — calling "
                         "this one again will keep failing."),
                error_kind="tool_call_cap",
            )))
            continue
        used[call.name] = used.get(call.name, 0) + 1

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
    return finished, deduped, capped


def run_agent(
    query: str,
    *,
    llm: LLMClient | None = None,
    registry: ToolRegistry | None = None,
    config: AgentConfig | None = None,
    on_step: Callable[[StepEvent], None] | None = None,
) -> AgentResult:
    """Answer ``query`` by letting the model drive the tool calls.

    ``on_step`` receives a :class:`StepEvent` as each phase completes; pass it to
    stream progress into a chat client. Optional and side-effect free.
    """
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
    draft = ""
    draft_findings: list[str] = []
    deduped_total = 0
    capped_total = 0
    error = ""
    forced_final = False
    report = grounding.GroundingReport()
    attribution_report = attribution.AttributionReport()

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
            # Strip the model's process narration *before* checking, not after:
            # the checks should verify the text the user is shown, and a repair
            # round that apologises in prose ("没有 IVV 的「1」这个数据") would
            # otherwise put figures into the answer that only exist because a
            # check complained about them.
            answer = presentation.strip_deliberation(response.text)
            report = grounding.check(answer, trajectory.observations_text())
            attribution_report = (attribution.check(answer, trajectory.tool_records())
                                  if config.attribution_check
                                  else attribution.AttributionReport())
            can_repair = config.grounding_repair and repairs < 1 and not budget_spent

            if report.ok and attribution_report.ok:
                stop_reason = "final_answer"
                forced_final = budget_spent
                break
            if not can_repair:
                stop_reason = ("final_answer_misattributed" if not attribution_report.ok
                               else "final_answer_ungrounded")
                forced_final = budget_spent
                break
            repairs += 1
            draft = answer
            draft_findings = (
                [f"无法溯源 {figure}" for figure in report.ungrounded]
                + [f"{entity}←{figure}(实为 {'/'.join(owners)})"
                   for entity, figure, owners in attribution_report.misattributed])
            reasons: list[str] = []
            if not report.ok:
                repaired_figures.extend(report.ungrounded)
                directives.append(grounding.repair_instruction(report))
                reasons.append(f"{len(report.ungrounded)} 个数字无法溯源")
            if not attribution_report.ok:
                directives.append(attribution.repair_instruction(attribution_report))
                reasons.append(attribution_report.summary())
            _notify(on_step, StepEvent(
                "repair", step_index, "初稿有问题，正在重写：" + "；".join(reasons),
                ok=False))
            _emit("agent_grounding_repair", ungrounded=len(report.ungrounded))
            continue

        # -- the model chose to act ----------------------------------------
        if response.text:
            note = extract_note(response.text)
            if note:
                trajectory.notes.append(note)
        _emit("agent_step", step=step_index,
              tools=[c.name for c in response.tool_calls])
        tool_names = tuple(c.name for c in response.tool_calls)
        _notify(on_step, StepEvent(
            "tools", step_index,
            (response.text or "").strip()[:160] or f"调用 {len(tool_names)} 个工具",
            tools=tool_names))

        results, deduped, capped = _execute_calls(
            response.tool_calls, registry, trajectory, config)
        step.results = results
        deduped_total += deduped
        capped_total += capped

        failed = [r.name for r in results if not r.ok]
        _notify(on_step, StepEvent(
            "observation", step_index,
            (f"{len(results) - len(failed)}/{len(results)} 个工具返回成功"
             + (f"；{', '.join(failed)} 失败，将改用其他工具" if failed else "")),
            tools=tuple(r.name for r in results),
            ok=not failed))

        if budget_spent:
            stop_reason = "budget_exhausted"
            forced_final = True
            break

    if not answer and stop_reason not in ("llm_error",):
        answer = ("（未能在预算内给出结论）已收集的观测："
                  + ", ".join(trajectory.distinct_tools()))

    _notify(on_step, StepEvent("final", len(trajectory.steps), "已生成回答",
                               ok=stop_reason.startswith("final_answer")))

    result = AgentResult(
        query=query,
        answer=answer,
        trajectory=trajectory,
        stop_reason=stop_reason,
        elapsed_ms=int((time.time() - started) * 1000),
        grounding=report,
        attribution=attribution_report,
        repairs=repairs,
        repaired_figures=repaired_figures,
        draft=draft,
        draft_findings=draft_findings,
        deduped_calls=deduped_total,
        capped_calls=capped_total,
        forced_final=forced_final,
        error=error,
    )
    _emit("agent_end", **{k: v for k, v in result.stats().items()
                          if isinstance(v, (int, float, str))})
    return result
