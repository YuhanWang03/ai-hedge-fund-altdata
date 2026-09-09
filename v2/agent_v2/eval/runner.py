"""Offline suite runner for the Agent V2 seed evaluation set."""

from __future__ import annotations

from dataclasses import dataclass

from v2.agent_v2.eval.answer_cases import ANSWER_CASES, AnswerCase
from v2.agent_v2.eval.cases import CASES, EvalCase
from v2.agent_v2.eval.fixtures import build_eval_registry, EvalSynthesizer
from v2.agent_v2.eval.scoring import AnswerScore, CaseScore, score_answer_case, score_case
from v2.agent_v2.orchestrator import AgentV2


@dataclass(frozen=True)
class SuiteReport:
    scores: tuple[CaseScore, ...]
    answer_scores: tuple[AnswerScore, ...] = ()

    @property
    def passed(self) -> int:
        return sum(score.passed for score in self.scores) + sum(score.passed for score in self.answer_scores)

    @property
    def total(self) -> int:
        return len(self.scores) + len(self.answer_scores)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def run_suite(cases: tuple[EvalCase, ...] = CASES, answer_cases: tuple[AnswerCase, ...] = ANSWER_CASES) -> SuiteReport:
    registry = build_eval_registry()
    agent = AgentV2(catalog=registry.catalog, registry=registry, synthesizer=EvalSynthesizer())
    scores = tuple(score_case(case, agent.run(case.query)) for case in cases)
    answer_scores = tuple(score_answer_case(case) for case in answer_cases)
    return SuiteReport(scores, answer_scores)


def render(report: SuiteReport) -> str:
    lines = [f"Agent V2 offline eval: {report.passed}/{report.total} ({report.pass_rate:.0%})"]
    for score in report.scores:
        mark = "PASS" if score.passed else "FAIL"
        lines.append(f"{mark} {score.case_id}: route={score.route_ok} recall={score.capability_recall:.0%} discipline={score.discipline_ok} status={score.status_ok} evidence={score.evidence_ok}")
    for score in report.answer_scores:
        mark = "PASS" if score.passed else "FAIL"
        lines.append(f"{mark} {score.case_id}: verdict={'ok' if score.verifier_ok else 'rejected'} expected={'ok' if score.expected_ok else 'rejected'} warnings={'; '.join(score.warnings) or '-'}")
    return "\n".join(lines)
