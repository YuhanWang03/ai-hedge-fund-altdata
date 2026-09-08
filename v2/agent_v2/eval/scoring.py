"""Capability-level scoring for one Agent V2 result."""

from __future__ import annotations

from dataclasses import dataclass

from v2.agent_v2.eval.cases import EvalCase
from v2.agent_v2.models import AgentResult


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    passed: bool
    route_ok: bool
    capability_recall: float
    discipline_ok: bool
    status_ok: bool
    answer_mode_ok: bool
    evidence_ok: bool
    called: tuple[str, ...]


def score_case(case: EvalCase, result: AgentResult) -> CaseScore:
    called = tuple(item.capability for item in result.results)
    required = set(case.required_capabilities)
    acquired = required.intersection(called)
    recall = len(acquired) / len(required) if required else 1.0
    route_ok = result.route.kind == case.expected_route
    discipline_ok = not set(case.forbidden_capabilities).intersection(called)
    status_ok = result.status in case.expected_statuses
    answer_mode_ok = case.expected_answer_mode is None or result.answer_mode == case.expected_answer_mode
    evidence_ok = result.verification.ok and (not called or bool(result.evidence) or result.status in {status for status in case.expected_statuses if status.value in {"queued", "waiting_confirmation"}})
    passed = route_ok and recall == 1.0 and discipline_ok and status_ok and answer_mode_ok and evidence_ok
    return CaseScore(case.id, passed, route_ok, recall, discipline_ok, status_ok, answer_mode_ok, evidence_ok, called)
