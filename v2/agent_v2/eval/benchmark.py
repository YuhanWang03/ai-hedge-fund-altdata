"""Run the ported V1 evaluation set against Agent V1 and Agent V2 side by side.

Three modes share one question set and one answer key:

``v1_baseline``
    V1's production single-hop path: the labelled intent, one responder call,
    no model.  The number every V2 mode has to beat.
``v2_rules``
    V2 with the rule planner and the deterministic synthesizer.  No model, so
    it measures routing, entity extraction and capability choice; its fact
    recall is a floor because the synthesizer only echoes evidence.
``v2_llm``
    V2 as deployed: LLM planner and LLM synthesizer behind the verifier.
    Needs a model; ``llm_factory`` can inject a scripted client for tests.

Every mode runs on recorded observations, so a score depends only on the code
under test and, for ``v2_llm``, the model.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from v2.agent.eval.scoring import fact_present, normalise
from v2.agent_v2.eval.benchmark_cases import DEV_CASES, HOLDOUT_CASES, BenchmarkCase, by_category
from v2.agent_v2.eval.benchmark_fixtures import RecordedCalls, build_benchmark_registry
from v2.agent_v2.llm import LLMEvidenceSynthesizer, StructuredLLMPlanner
from v2.agent_v2.orchestrator import AgentV2, AgentV2Config
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer

MODES = ("v1_baseline", "v2_rules", "v2_llm")


@dataclass
class CountingLLM:
    """Wrap any LLM client to count calls and tokens for the cost columns."""

    inner: Any
    calls: int = 0
    tokens: int = 0

    def complete(self, messages, tools=None):
        self.calls += 1
        response = self.inner.complete(messages, tools)
        self.tokens += int(getattr(response, "prompt_tokens", 0) or 0) + int(getattr(response, "completion_tokens", 0) or 0)
        return response


@dataclass(frozen=True)
class BenchmarkScore:
    case_id: str
    category: str
    mode: str
    holdout: bool
    tool_recall: float
    fact_recall: float
    missing_tools: tuple[str, ...]
    missing_facts: tuple[str, ...]
    forbidden_hit: tuple[str, ...]
    waste: tuple[str, ...]
    unmapped_tools: tuple[str, ...]
    grounded: bool
    verify_outcome: str
    status: str
    route: str
    tool_calls: int
    llm_calls: int
    tokens: int
    elapsed_ms: int
    stop_reason: str
    error: str
    answer: str
    called: tuple[str, ...]

    @property
    def answer_correct(self) -> bool:
        return self.tool_recall == 1.0 and self.fact_recall == 1.0 and not self.forbidden_hit and not self.error

    @property
    def passed(self) -> bool:
        return self.answer_correct and self.grounded

    @property
    def capability_gap(self) -> bool:
        return bool(self.unmapped_tools)

    def failure_reason(self) -> str:
        if self.error:
            return f"运行错误：{self.error[:80]}"
        if self.missing_tools:
            return "缺能力：" + ", ".join(self.missing_tools)
        if self.missing_facts:
            return "缺事实：" + ", ".join(self.missing_facts[:3])
        if self.forbidden_hit:
            return "错误归属：" + ", ".join(self.forbidden_hit[:2])
        if not self.grounded:
            return f"校验未通过（{self.verify_outcome}）"
        return ""


@dataclass
class ModeReport:
    mode: str
    scores: list[BenchmarkScore] = field(default_factory=list)
    repeat: int = 1

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def passed(self) -> int:
        return sum(1 for score in self.scores if score.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def mean(self, attribute: str) -> float:
        values = [float(getattr(score, attribute)) for score in self.scores]
        return sum(values) / len(values) if values else 0.0

    def rate(self, predicate: Callable[[BenchmarkScore], bool]) -> float:
        return sum(1 for score in self.scores if predicate(score)) / self.total if self.total else 0.0

    def verify_outcomes(self) -> Counter:
        return Counter(score.verify_outcome for score in self.scores if score.verify_outcome)

    def stability(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for score in self.scores:
            passed, total = out.get(score.case_id, (0, 0))
            out[score.case_id] = (passed + int(score.passed), total + 1)
        return out

    def stable_failures(self) -> list[str]:
        return sorted(case_id for case_id, (passed, _) in self.stability().items() if passed == 0)

    def by_category(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for score in self.scores:
            passed, total = out.get(score.category, (0, 0))
            out[score.category] = (passed + int(score.passed), total + 1)
        return out


def _score(case: BenchmarkCase, *, mode: str, answer: str, called: Iterable[str], grounded: bool, verify_outcome: str = "", status: str = "", route: str = "", tool_calls: int = 0, llm_calls: int = 0, tokens: int = 0, elapsed_ms: int = 0, stop_reason: str = "", error: str = "") -> BenchmarkScore:
    called_set = set(called)
    missing_tools = tuple(name for name in case.must_call if name not in called_set)
    denominator = len(case.must_call) + len(case.unmapped_tools)
    tool_recall = 1.0 if not denominator else 1.0 - (len(missing_tools) + len(case.unmapped_tools)) / denominator
    required = tuple(case.facts) + tuple(case.behaviors)
    missing_facts = tuple(forms[0] for forms in required if not fact_present(forms, answer))
    fact_recall = 1.0 if not required else 1.0 - len(missing_facts) / len(required)
    haystack = normalise(answer)
    forbidden_hit = tuple(value for value in case.forbidden if normalise(value) in haystack)
    return BenchmarkScore(
        case_id=case.id,
        category=case.category,
        mode=mode,
        holdout=case.holdout,
        tool_recall=tool_recall,
        fact_recall=fact_recall,
        missing_tools=missing_tools,
        missing_facts=missing_facts,
        forbidden_hit=forbidden_hit,
        waste=tuple(sorted(called_set & set(case.wasteful))),
        unmapped_tools=case.unmapped_tools,
        grounded=grounded,
        verify_outcome=verify_outcome,
        status=status,
        route=route,
        tool_calls=tool_calls,
        llm_calls=llm_calls,
        tokens=tokens,
        elapsed_ms=elapsed_ms,
        stop_reason=stop_reason,
        error=error,
        answer=answer,
        called=tuple(called),
    )


# -- modes ------------------------------------------------------------------


def run_v1_baseline(case: BenchmarkCase) -> BenchmarkScore:
    """V1's single-hop production path on V1's own fixtures and tool names."""

    from v2.agent.baseline import run_baseline
    from v2.agent.eval.fixtures import build_eval_registry
    from v2.agent.eval.runner import _parsed

    started = time.time()
    try:
        result = run_baseline(case.query, classifier=lambda _text: _parsed(case.v1), registry=build_eval_registry())
    except Exception as exc:  # noqa: BLE001 — a crash is a scored failure
        return _score(case, mode="v1_baseline", answer="", called=(), grounded=False, error=f"{type(exc).__name__}: {exc}", elapsed_ms=int((time.time() - started) * 1000))
    called = [result.tool] if result.tool else []
    v1_missing = [tool for tool in case.v1_must_call if tool not in called]
    score = _score(case, mode="v1_baseline", answer=result.answer, called=called, grounded=True, status="single_hop", route="single_hop", tool_calls=len(called), elapsed_ms=result.elapsed_ms, error=result.error)
    # Score V1 in its own vocabulary: the ported must_call would credit a
    # single research card for every card the question needs.
    v1_recall = 1.0 if not case.v1_must_call else 1.0 - len(v1_missing) / len(case.v1_must_call)
    return BenchmarkScore(**{**asdict(score), "tool_recall": v1_recall, "missing_tools": tuple(v1_missing), "unmapped_tools": ()})


def _v2_agent(mode: str, llm_factory: Callable[[], Any] | None) -> tuple[AgentV2, RecordedCalls, CountingLLM | None, LLMEvidenceSynthesizer | None]:
    registry, calls = build_benchmark_registry()
    if mode == "v2_rules":
        return AgentV2(catalog=registry.catalog, registry=registry, synthesizer=EvidenceSummarySynthesizer(), config=AgentV2Config(max_seconds=60)), calls, None, None
    if llm_factory is None:
        from v2.agent.llm import build_llm

        llm_factory = build_llm
    llm = CountingLLM(llm_factory())
    synthesizer = LLMEvidenceSynthesizer(llm, catalog=registry.catalog)
    agent = AgentV2(catalog=registry.catalog, registry=registry, planner=StructuredLLMPlanner(llm, registry.catalog), synthesizer=synthesizer, config=AgentV2Config(max_seconds=120))
    return agent, calls, llm, synthesizer


def run_v2(case: BenchmarkCase, *, mode: str, llm_factory: Callable[[], Any] | None = None) -> BenchmarkScore:
    agent, calls, llm, synthesizer = _v2_agent(mode, llm_factory)
    started = time.time()
    try:
        result = agent.run(case.query)
    except Exception as exc:  # noqa: BLE001 — a crash is a scored failure
        return _score(case, mode=mode, answer="", called=calls.names(), grounded=False, error=f"{type(exc).__name__}: {exc}", elapsed_ms=int((time.time() - started) * 1000))
    outcome = synthesizer.last_outcome if synthesizer is not None else "deterministic"
    return _score(
        case,
        mode=mode,
        answer=result.answer,
        called=calls.names(),
        grounded=result.verification.ok,
        verify_outcome=outcome,
        status=result.status.value,
        route=result.route.kind.value,
        tool_calls=len(calls.calls),
        llm_calls=llm.calls if llm else 0,
        tokens=llm.tokens if llm else 0,
        elapsed_ms=result.elapsed_ms,
        stop_reason=result.stop_reason,
        error=result.error,
    )


def run_mode(mode: str, cases: tuple[BenchmarkCase, ...], *, repeat: int = 1, llm_factory: Callable[[], Any] | None = None, on_case: Callable[[BenchmarkScore], None] | None = None) -> ModeReport:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if mode != "v2_llm":
        repeat = 1  # deterministic modes cannot flake
    report = ModeReport(mode=mode, repeat=repeat)
    for case in cases:
        for _ in range(repeat):
            score = run_v1_baseline(case) if mode == "v1_baseline" else run_v2(case, mode=mode, llm_factory=llm_factory)
            report.scores.append(score)
            if on_case is not None:
                on_case(score)
    return report


def run_benchmark(modes: Iterable[str] = ("v1_baseline", "v2_rules"), *, holdout: bool = False, repeat: int = 1, llm_factory: Callable[[], Any] | None = None) -> list[ModeReport]:
    cases = HOLDOUT_CASES if holdout else DEV_CASES
    return [run_mode(mode, cases, repeat=repeat, llm_factory=llm_factory) for mode in modes]


# -- rendering ----------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value:.0%}"


def render_comparison(reports: list[ModeReport]) -> str:
    rows = [
        ("通过率", lambda r: _pct(r.pass_rate)),
        ("工具召回", lambda r: _pct(r.mean("tool_recall"))),
        ("事实召回", lambda r: _pct(r.mean("fact_recall"))),
        ("错误归属命中", lambda r: str(sum(1 for s in r.scores if s.forbidden_hit))),
        ("校验通过率", lambda r: _pct(r.rate(lambda s: s.grounded))),
        ("校验结果", lambda r: ", ".join(f"{k}={v}" for k, v in sorted(r.verify_outcomes().items())) or "-"),
        ("能力缺口用例", lambda r: str(sum(1 for s in r.scores if s.capability_gap))),
        ("超预算", lambda r: str(sum(1 for s in r.scores if s.stop_reason == "deadline"))),
        ("工具调用 / 例", lambda r: f"{r.mean('tool_calls'):.1f}"),
        ("LLM 调用 / 例", lambda r: f"{r.mean('llm_calls'):.1f}"),
        ("token / 例", lambda r: f"{r.mean('tokens'):.0f}"),
        ("耗时 ms / 例", lambda r: f"{r.mean('elapsed_ms'):.0f}"),
        ("稳定失败", lambda r: str(len(r.stable_failures()))),
    ]
    header = f"{'':14}" + "".join(f"{r.mode:>14}" for r in reports)
    lines = [header, "─" * len(header)]
    for label, cell in rows:
        lines.append(f"{label:14}" + "".join(f"{cell(r):>14}" for r in reports))
    return "\n".join(lines)


def render_categories(reports: list[ModeReport]) -> str:
    categories = list(dict.fromkeys(score.category for report in reports for score in report.scores))
    header = f"{'类别':14}" + "".join(f"{r.mode:>14}" for r in reports)
    lines = [header, "─" * len(header)]
    for category in categories:
        cells = []
        for report in reports:
            passed, total = report.by_category().get(category, (0, 0))
            cells.append(f"{passed}/{total}")
        lines.append(f"{category:14}" + "".join(f"{cell:>14}" for cell in cells))
    return "\n".join(lines)


def render_failures(report: ModeReport, limit: int = 30) -> str:
    seen: set[str] = set()
    lines = [f"[{report.mode}] 失败用例（最多 {limit} 条）"]
    for score in report.scores:
        if score.passed or score.case_id in seen:
            continue
        seen.add(score.case_id)
        gap = " ⚠能力缺口" if score.capability_gap else ""
        lines.append(f"  {score.case_id:5} {score.category:14} {score.failure_reason()}{gap}  called={list(score.called)}")
        if len(lines) > limit:
            break
    return "\n".join(lines)


def render(reports: list[ModeReport], *, failures: bool = True) -> str:
    parts = [render_comparison(reports), "", render_categories(reports)]
    if failures:
        parts.extend(["", *(render_failures(report) for report in reports)])
    return "\n".join(parts)


def to_json(reports: list[ModeReport]) -> dict[str, Any]:
    return {
        "modes": [
            {
                "mode": report.mode,
                "repeat": report.repeat,
                "pass_rate": report.pass_rate,
                "scores": [asdict(score) for score in report.scores],
            }
            for report in reports
        ]
    }


def gap_summary(cases: tuple[BenchmarkCase, ...] = DEV_CASES) -> dict[str, list[str]]:
    """Which V1 tools the port could not map, and which cases they block."""

    gaps: dict[str, list[str]] = {}
    for case in cases:
        for tool in case.unmapped_tools:
            gaps.setdefault(tool, []).append(case.id)
    return gaps


__all__ = ["MODES", "BenchmarkScore", "ModeReport", "run_benchmark", "run_mode", "render", "to_json", "gap_summary", "by_category"]
