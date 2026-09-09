"""The scaffold every sub-agent shares: a bounded JSON-action loop behind a capability.

A sub-agent is a capability whose handler lets a model choose its next
tool call for a few rounds.  What makes it safe to sit beside pure
functions is what this module owns: a hard cap on rounds and seconds,
the coordinator's remaining wall clock as an outer bound, a forced
finish when the rounds run out, and a diagnostic record of what
happened.  Domain knowledge (which tools, which prompt, how to turn a
finish into evidence) lives in the subclass.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from v2.agent_v2.execution import ExecutionContext

#: Seconds kept back from the coordinator's remaining budget so the
#: envelope can still be built and ingested after the loop stops.
BUDGET_MARGIN_SECONDS = 5.0
#: Below this much remaining time the loop does not start at all.
MINIMUM_LOOP_SECONDS = 8.0


@dataclass
class LoopLimits:
    max_rounds: int = 4
    max_seconds: float = 60.0
    #: Overrides ``max_seconds`` when the coordinator has less time left.
    outer_seconds: float | None = None

    @property
    def seconds(self) -> float:
        if self.outer_seconds is None:
            return self.max_seconds
        return max(0.0, min(self.max_seconds, self.outer_seconds - BUDGET_MARGIN_SECONDS))


@dataclass
class LoopOutcome:
    """What the loop did, for the envelope's metrics and the answer's limitations."""

    finished: bool = False
    final: dict[str, Any] = field(default_factory=dict)
    rounds: int = 0
    calls: int = 0
    elapsed_ms: int = 0
    #: ``finished``, ``rounds``, ``time``, ``no_model`` or ``no_budget``.
    stop_reason: str = ""
    seconds_allowed: float = 0.0

    @property
    def note(self) -> str:
        return {
            "rounds": "达到轮次上限",
            "time": "达到时间上限",
            "no_model": "未配置模型",
            "no_budget": "协调者剩余时间不足，未启动",
        }.get(self.stop_reason, "")


def limits_for(context: ExecutionContext | None, *, max_rounds: int, max_seconds: float) -> LoopLimits:
    """The loop's limits with the coordinator's remaining wall clock as an outer bound."""

    outer = None
    if context is not None:
        try:
            outer = float(context.remaining_seconds())
        except Exception:  # noqa: BLE001 — a context without a clock imposes no bound
            outer = None
    return LoopLimits(max_rounds=max(1, max_rounds), max_seconds=max(5.0, max_seconds), outer_seconds=outer)


def strip_fence(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        value = "\n".join(lines).strip()
    start, end = value.find("{"), value.rfind("}")
    return value[start : end + 1] if start >= 0 and end > start else value


class BoundedLoop:
    """Drive a model through JSON actions until it finishes or a limit stops it.

    Subclasses implement :meth:`handle` — apply one non-finish action and
    return True when it did something (a bad action returns False after
    appending a correction message).  The loop itself never reads the
    domain: it only knows ``{"action": "finish", ...}``.
    """

    def __init__(self, llm: Any, limits: LoopLimits) -> None:
        self.llm = llm
        self.limits = limits

    def handle(self, action: dict[str, Any], messages: list[dict[str, str]]) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, system: str, task: str, *, finish_prompt: str) -> LoopOutcome:
        outcome = LoopOutcome(seconds_allowed=self.limits.seconds)
        started = time.monotonic()
        if self.llm is None:
            outcome.stop_reason = "no_model"
            return outcome
        if self.limits.seconds < MINIMUM_LOOP_SECONDS:
            outcome.stop_reason = "no_budget"
            return outcome
        messages: list[dict[str, str]] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        stop = "rounds"
        for _ in range(self.limits.max_rounds):
            if time.monotonic() - started > self.limits.seconds:
                stop = "time"
                break
            outcome.rounds += 1
            outcome.calls += 1
            action = self.step(messages)
            if action is None:
                continue
            if action.get("action") == "finish":
                outcome.finished, outcome.final, stop = True, action, "finished"
                break
            self.handle(action, messages)
        if not outcome.finished and stop == "rounds" and time.monotonic() - started <= self.limits.seconds:
            # One last call that may only finish: what the loop gathered is
            # not thrown away because it kept exploring.
            messages.append({"role": "user", "content": finish_prompt})
            outcome.calls += 1
            action = self.step(messages)
            if action is not None and action.get("action") == "finish":
                outcome.finished, outcome.final, stop = True, action, "finished"
        outcome.stop_reason = stop
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def step(self, messages: list[dict[str, str]]) -> dict[str, Any] | None:
        """One model turn parsed as an action; a bad turn is answered and returns None."""

        try:
            response = self.llm.complete(messages, None)
            action = json.loads(strip_fence(response.text))
            if not isinstance(action, dict):
                raise ValueError("action must be an object")
        except Exception as exc:  # noqa: BLE001 — a bad turn is data for the envelope
            messages.append({"role": "user", "content": f"上一轮输出无法解析（{type(exc).__name__}），请只输出 JSON。"})
            return None
        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        return action
