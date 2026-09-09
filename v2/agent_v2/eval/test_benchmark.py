"""Contract tests for the V1→V2 benchmark port; no model or network required."""

from __future__ import annotations

import json

from v2.agent.llm import LLMResponse, ScriptedLLM
from v2.agent_v2.catalog import default_catalog
from v2.agent_v2.eval.benchmark import gap_summary, render, run_mode, run_v2, to_json
from v2.agent_v2.eval.benchmark_cases import DEV_CASES, HOLDOUT_CASES, TOOL_MAP
from v2.agent_v2.eval.benchmark_fixtures import build_benchmark_registry
from v2.agent_v2.execution import ExecutionContext
from v2.agent_v2.models import BudgetClass, NormalizedRequest, PlanTask


def test_port_keeps_every_v1_case_and_maps_tools_into_the_catalog():
    assert len(DEV_CASES) == 89 and len(HOLDOUT_CASES) == 15
    catalog = default_catalog()
    for case in DEV_CASES + HOLDOUT_CASES:
        assert case.query and case.category
        for name in case.must_call:
            assert catalog.get(name) is not None, (case.id, name)
        assert not set(case.must_call) & set(case.wasteful)
    assert all(target == "" or default_catalog().get(target) is not None for target in TOOL_MAP.values())
    assert gap_summary(DEV_CASES) == {"macro_view": [case.id for case in DEV_CASES if "macro_view" in case.v1_must_call]}


def test_benchmark_registry_serves_v1_cards_for_every_ported_capability():
    registry, calls = build_benchmark_registry()
    context = ExecutionContext("bench", NormalizedRequest("q", "q"), BudgetClass.STANDARD, allow_mutations=True)
    for case in DEV_CASES + HOLDOUT_CASES:
        for name in case.must_call:
            assert registry.registered(name), (case.id, name)
    move = registry.execute(PlanTask("m", "market.explain_move", {"ticker": "NVDA"}), context)
    assert move.ok and "3.85%" in move.summary and move.evidence
    research = registry.execute(PlanTask("r", "research.stock", {"ticker": "CRWD", "focus": "filings"}), context)
    assert "CFO" in research.summary and research.evidence[0].metadata["module"] == "eight_k_view"
    broken = registry.execute(PlanTask("r", "research.stock", {"ticker": "SMCI", "focus": "filings"}), context)
    assert broken.status.value == "partial_data" and any("timed out" in value for value in broken.limitations)
    pnl = registry.execute(PlanTask("p", "account.performance", {"period": "week"}), context)
    assert pnl.ok
    assert [name for name, _ in calls.calls] == ["market.explain_move", "research.stock", "research.stock", "account.performance"]


def test_rules_mode_scores_every_case_without_raising_and_reproduces_the_v1_baseline():
    rules = run_mode("v2_rules", DEV_CASES[:12])
    assert rules.total == 12 and not any(score.error for score in rules.scores)
    assert all(score.verify_outcome == "deterministic" for score in rules.scores)
    baseline = run_mode("v1_baseline", DEV_CASES[:12])
    first = baseline.scores[0]
    assert first.case_id == "s01" and first.passed and first.called == ("explain_move",)
    text = render([baseline, rules])
    assert "通过率" in text and "single_lookup" in text
    payload = to_json([baseline, rules])
    assert json.dumps(payload, ensure_ascii=False)
    assert payload["modes"][1]["scores"][0]["case_id"] == "s01"


def test_llm_mode_counts_calls_and_reports_the_verifier_outcome():
    case = next(case for case in DEV_CASES if case.id == "s01")
    registry, _ = build_benchmark_registry()
    context = ExecutionContext("bench", NormalizedRequest("q", "q"), BudgetClass.STANDARD)
    move = registry.execute(PlanTask("m", "market.explain_move", {"ticker": "NVDA"}), context)
    evidence_id = move.evidence[0].id
    scripted = ScriptedLLM([LLMResponse(text=f"NVDA 今日上涨 3.85%，相对 SMH 强势。[{evidence_id}]")])
    score = run_v2(case, mode="v2_llm", llm_factory=lambda: scripted)
    assert score.passed, score.failure_reason()
    assert score.llm_calls == 1 and score.verify_outcome == "clean"
    repaired = ScriptedLLM([LLMResponse(text=f"NVDA 今日上涨 9.99%。[{evidence_id}]"), LLMResponse(text=f"NVDA 今日上涨 3.85%，同期 SMH 走强。[{evidence_id}]")])
    score = run_v2(case, mode="v2_llm", llm_factory=lambda: repaired)
    assert score.passed and score.llm_calls == 2 and score.verify_outcome == "repaired"
    stubborn = ScriptedLLM([LLMResponse(text=f"NVDA 今日上涨 9.99%。[{evidence_id}]"), LLMResponse(text=f"NVDA 今日上涨 8.88%。[{evidence_id}]")])
    score = run_v2(case, mode="v2_llm", llm_factory=lambda: stubborn)
    assert score.verify_outcome == "fallback" and score.grounded and "3.85%" in score.answer
