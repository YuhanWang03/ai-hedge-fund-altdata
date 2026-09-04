"""Scoring — turning one run into numbers that say *why* it failed.

A single pass/fail per case is almost useless for improving anything: a run that
called the wrong tools and a run that called the right ones but dropped a fact
look identical, and they need opposite fixes. So every case is scored on four
independent axes, and ``passed`` is their conjunction:

* **tool recall** — did it fetch what the answer needs? Extra tools are not
  penalised here; they are charged to the cost metrics, where over-calling
  actually costs something.
* **fact recall** — did the required facts survive into the reply? Each fact is
  a set of acceptable surface forms, so this measures correctness rather than
  phrasing.
* **discipline** — did it call a tool the case marks as wrong or wasteful, or
  emit a string the case forbids (the classic being a figure borrowed from a
  different ticker when the real one has no data).
* **grounding** — every figure traces to an observation. An answer with an
  invented number is not a correct answer, so this is part of ``passed`` rather
  than a footnote.

The baseline's grounding is 1.0 by construction (it returns the card verbatim
and writes nothing), which is a real advantage of that design and is left
visible rather than normalised away.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from v2.agent.eval.cases import CATEGORIES, EvalCase

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lowercase, drop thousands separators, collapse whitespace.

    Removing commas is what lets a case assert ``184,320.55`` and still match a
    model that wrote ``184320.55``. Whitespace is collapsed rather than removed
    so that English quotes keep their word boundaries.
    """
    return _WS.sub(" ", (text or "").replace(",", "").lower()).strip()


def fact_present(forms: Iterable[str], answer: str) -> bool:
    """True when any acceptable surface form of one fact appears."""
    haystack = normalise(answer)
    return any(normalise(form) in haystack for form in forms if form)


@dataclass
class CaseScore:
    case_id: str
    category: str
    mode: str

    tool_recall: float = 0.0
    fact_recall: float = 0.0
    grounded: bool = True
    missing_tools: tuple[str, ...] = ()
    missing_facts: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    forbidden_hit: tuple[str, ...] = ()
    #: The figures that failed to trace. "数字无法溯源" without naming them is
    #: the same unactionable verdict this package criticises elsewhere.
    ungrounded: tuple[str, ...] = ()
    #: {kind: [figures]} — why each rejected figure failed to trace.
    ungrounded_kinds: dict[str, list[str]] = field(default_factory=dict)

    tool_calls: int = 0
    llm_calls: int = 0
    tokens: int = 0
    elapsed_ms: int = 0
    overspend: bool = False

    path: str = ""
    path_correct: bool = True
    stop_reason: str = ""
    error: str = ""

    @property
    def passed(self) -> bool:
        return (self.tool_recall >= 1.0
                and self.fact_recall >= 1.0
                and not self.violations
                and not self.forbidden_hit
                and self.grounded
                and not self.error)

    def failure_reason(self) -> str:
        """The single most actionable reason, for the per-case failure list."""
        if self.error:
            return f"运行错误：{self.error}"
        if self.tool_recall < 1.0:
            return f"工具漏调：{', '.join(self.missing_tools)}"
        if self.violations:
            return f"调用了不该调的：{', '.join(self.violations)}"
        if self.forbidden_hit:
            return f"输出了禁止内容：{', '.join(self.forbidden_hit)}"
        if not self.grounded:
            figures = ", ".join(self.ungrounded[:6]) if self.ungrounded else "未记录"
            return f"数字无法溯源：{figures}"
        if self.fact_recall < 1.0:
            return f"事实缺失：{', '.join(self.missing_facts)}"
        return ""


def score_case(
    case: EvalCase,
    *,
    mode: str,
    answer: str,
    tools_called: Iterable[str],
    grounded: bool = True,
    ungrounded: Iterable[str] = (),
    ungrounded_kinds: dict[str, list[str]] | None = None,
    tool_calls: int = 0,
    llm_calls: int = 0,
    tokens: int = 0,
    elapsed_ms: int = 0,
    path: str = "",
    stop_reason: str = "",
    error: str = "",
) -> CaseScore:
    called = set(tools_called)

    missing_tools = tuple(t for t in case.must_call if t not in called)
    tool_recall = 1.0 if not case.must_call else 1.0 - len(missing_tools) / len(case.must_call)

    # Data facts and behavioural requirements gate a case identically; they
    # differ only in whether the answer key check expects to find them in the
    # fixtures.
    required = tuple(case.facts) + tuple(case.behaviors)
    missing_facts = tuple(forms[0] for forms in required if not fact_present(forms, answer))
    fact_recall = 1.0 if not required else 1.0 - len(missing_facts) / len(required)

    haystack = normalise(answer)
    forbidden_hit = tuple(f for f in case.forbidden if normalise(f) in haystack)

    return CaseScore(
        case_id=case.id, category=case.category, mode=mode,
        tool_recall=tool_recall, fact_recall=fact_recall, grounded=grounded,
        missing_tools=missing_tools, missing_facts=missing_facts,
        violations=tuple(sorted(called & set(case.must_not_call))),
        forbidden_hit=forbidden_hit, ungrounded=tuple(ungrounded),
        ungrounded_kinds=dict(ungrounded_kinds or {}),
        tool_calls=tool_calls, llm_calls=llm_calls, tokens=tokens,
        elapsed_ms=elapsed_ms, overspend=tool_calls > case.max_tool_calls,
        path=path, path_correct=(not path or path == case.expected_path),
        stop_reason=stop_reason, error=error,
    )


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


@dataclass
class SuiteReport:
    mode: str
    scores: list[CaseScore] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scores if s.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for category in CATEGORIES:
            group = [s for s in self.scores if s.category == category]
            if group:
                out[category] = (sum(1 for s in group if s.passed), len(group))
        return out

    def summary(self) -> dict[str, Any]:
        tokens = [float(s.tokens) for s in self.scores]
        latency = [float(s.elapsed_ms) for s in self.scores]
        passed = self.passed
        return {
            "mode": self.mode,
            "total": self.total,
            "passed": passed,
            "pass_rate": self.pass_rate,
            "tool_recall": _mean([s.tool_recall for s in self.scores]),
            "fact_recall": _mean([s.fact_recall for s in self.scores]),
            "grounded_rate": _mean([1.0 if s.grounded else 0.0 for s in self.scores]),
            "violation_rate": _mean([1.0 if s.violations else 0.0 for s in self.scores]),
            "overspend_rate": _mean([1.0 if s.overspend else 0.0 for s in self.scores]),
            "routing_accuracy": _mean([1.0 if s.path_correct else 0.0 for s in self.scores]),
            "ungrounded_kinds": self.ungrounded_breakdown(),
            "mean_tool_calls": _mean([float(s.tool_calls) for s in self.scores]),
            "mean_llm_calls": _mean([float(s.llm_calls) for s in self.scores]),
            "total_tokens": int(sum(tokens)),
            "median_tokens": _median(tokens),
            "median_latency_ms": _median(latency),
            # The metric that decides whether a mode is worth its cost.
            "tokens_per_pass": (sum(tokens) / passed) if passed else float("inf"),
        }

    def ungrounded_breakdown(self) -> dict[str, int]:
        """How many rejected figures of each kind, across the whole suite."""
        counts: dict[str, int] = {}
        for score in self.scores:
            for kind, figures in score.ungrounded_kinds.items():
                counts[kind] = counts.get(kind, 0) + len(figures)
        return counts

    def failures(self) -> list[CaseScore]:
        return [s for s in self.scores if not s.passed]
