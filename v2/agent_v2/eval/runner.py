"""Offline suite runner for the Agent V2 seed evaluation set."""

from __future__ import annotations

from dataclasses import dataclass

from v2.agent_v2.eval.cases import CASES, EvalCase
from v2.agent_v2.eval.fixtures import build_eval_registry, EvalSynthesizer
from v2.agent_v2.eval.scoring import CaseScore, score_case
from v2.agent_v2.orchestrator import AgentV2


@dataclass(frozen=True)
class SuiteReport:
    scores: tuple[CaseScore, ...]

    @property
    def passed(self) -> int:
        return sum(score.passed for score in self.scores)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def run_suite(cases: tuple[EvalCase, ...] = CASES) -> SuiteReport:
    registry = build_eval_registry()
    agent = AgentV2(catalog=registry.catalog, registry=registry, synthesizer=EvalSynthesizer())
    scores = tuple(score_case(case, agent.run(case.query)) for case in cases)
    return SuiteReport(scores)


def render(report: SuiteReport) -> str:
    lines = [f"Agent V2 offline eval: {report.passed}/{report.total} ({report.pass_rate:.0%})"]
    for score in report.scores:
        mark = "PASS" if score.passed else "FAIL"
        lines.append(f"{mark} {score.case_id}: route={score.route_ok} recall={score.capability_recall:.0%} discipline={score.discipline_ok} status={score.status_ok} evidence={score.evidence_ok}")
    return "\n".join(lines)
