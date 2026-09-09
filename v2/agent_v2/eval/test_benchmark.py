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
    assert gap_summary(DEV_CASES) == {}


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


def test_engine_fixtures_synthesize_production_shaped_envelopes_offline():
    from v2.agent_v2.eval.engine_fixtures import PROFILES, synthesize_market_envelope, synthesize_research_envelope
    from v2.agent_v2.models import AnswerMode
    from v2.agent_v2.verification import verify_answer

    research = synthesize_research_envelope("NVDA", "overview")
    assert research.ok and research.run_id.startswith("synthetic-nvda")
    assert any(item.metadata.get("citation_kind") == "metrics" for item in research.evidence)
    assert any("55.3%" in item.claim for item in research.evidence)
    assert synthesize_research_envelope("NVDA", "overview") == research  # deterministic
    for ticker in ("NVDA", "SMCI"):
        for capability in ("market.performance", "market.explain_move"):
            envelope = synthesize_market_envelope(capability, ticker)
            assert envelope.ok, (capability, ticker, envelope.errors)
            assert verify_answer(envelope.metadata["narrative"], envelope.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[envelope]).ok
    performance = synthesize_market_envelope("market.performance", "ARM")
    move = synthesize_market_envelope("market.explain_move", "ARM")
    assert performance.metrics["close"] == move.metrics["price"]
    assert len(PROFILES) >= 11


def test_recorded_store_round_trips_envelopes_and_wins_over_synthesis(tmp_path):
    from v2.agent_v2.eval.engine_fixtures import synthesize_research_envelope
    from v2.agent_v2.eval.recorded import RecordedStore, envelope_from_dict, envelope_to_dict

    envelope = synthesize_research_envelope("AMD", "filings")
    assert envelope_from_dict(envelope_to_dict(envelope)).to_dict() == envelope.to_dict()
    store = RecordedStore(tmp_path)
    assert store.load("research.stock", "AMD:filings") is None
    store.save("research.stock", "AMD:filings", envelope)
    reloaded = RecordedStore(tmp_path)
    assert reloaded.keys("research.stock") == ["AMD:filings"]
    assert reloaded.load("research.stock", "AMD:filings").to_dict() == envelope.to_dict()
    assert reloaded.summary() == {"research.stock": 1}


def test_record_fixtures_cli_writes_synthetic_envelopes(tmp_path, capsys):
    from v2.agent_v2.record_fixtures import main

    assert main(["--synthetic", "--tickers", "NVDA", "--focuses", "overview,risk", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "wrote 4 envelope(s)" in out
    assert (tmp_path / "research.stock.json").is_file() and (tmp_path / "market.explain_move.json").is_file()


def test_engine_fixture_mode_runs_the_real_adapters_and_scores_without_v1_fact_keys():
    from v2.agent_v2.eval.benchmark import run_mode

    cases = tuple(case for case in DEV_CASES if case.id in {"s01", "p01", "m02", "r04"})
    report = run_mode("v2_rules", cases, fixtures="engine")
    assert report.mode == "v2_rules@engine"
    by_id = {score.case_id: score for score in report.scores}
    assert not any(score.error for score in report.scores)
    assert all(not score.keyed for score in report.scores)
    assert by_id["p01"].passed  # two focuses on one ticker share engine evidence ids without conflict
    assert by_id["m02"].called.count("research.stock") >= 2  # fan-out over holdings
    assert by_id["s01"].grounded
