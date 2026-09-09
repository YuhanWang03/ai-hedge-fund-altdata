"""Contract tests for the initial Agent V2 framework; no API keys required."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from v2.agent.llm import LLMResponse, ScriptedLLM
from v2.agent_v2.adapters.lab import register_lab_capabilities
from v2.agent_v2.adapters.market import _observation_state, register_market_capabilities
from v2.agent_v2.adapters.research import register_research_capabilities
from v2.agent_v2.adapters.tavily_web import TavilyWebSearchPort
from v2.agent_v2.adapters.web import register_web_capability
from v2.agent_v2.adapters.workspace_lab import LabBinding, WorkspaceLabPort
from v2.agent_v2.catalog import default_catalog
from v2.agent_v2.eval.runner import run_suite
from v2.agent_v2.eval.scenario_cases import PORTFOLIO_CARD as _PORTFOLIO_CARD, build_drawdown_registry as _framed_registry
from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext, ExecutionEngine, PlanValidationError
from v2.agent_v2.interfaces.telegram import TelegramFacade, TelegramMessage
from v2.agent_v2.interfaces.web import WebFacade, WebRequest
from v2.agent_v2.llm import LLMEvidenceSynthesizer, StructuredLLMPlanner
from v2.agent_v2.models import (
    AnswerMode,
    BudgetClass,
    EvidenceItem,
    ExecutionPlan,
    NormalizedRequest,
    PlanTask,
    ResultStatus,
    RouteKind,
    RunStatus,
    ToolEnvelope,
)
from v2.agent_v2.orchestrator import AgentV2, AgentV2Config
from v2.agent_v2.planning import RulePlanner
from v2.agent_v2.routing import normalize_request, route
from v2.agent_v2.session import ShortTermSession
from v2.agent_v2.synthesis import EvidenceSummarySynthesizer
from v2.agent_v2.verification import verify_answer


def _context() -> ExecutionContext:
    return ExecutionContext("test-run", NormalizedRequest("q", "q"), BudgetClass.STANDARD)


def test_router_separates_knowledge_research_lab_and_commands():
    assert route(normalize_request("什么是自由现金流？")).kind == RouteKind.GENERAL_KNOWLEDGE
    assert route(normalize_request("比较 NVDA 和 AMD 的风险")).kind == RouteKind.RESEARCH
    assert route(normalize_request("回测 NVDA 动量策略")).kind == RouteKind.LAB
    assert route(normalize_request("把 NVDA 加入关注列表")).kind == RouteKind.COMMAND


def test_recent_stock_performance_uses_market_data_instead_of_fundamentals():
    request = normalize_request("AMD最近表现如何？")
    plan = RulePlanner().plan(request, route(request))
    assert plan.tasks[0].capability == "market.performance"
    assert plan.tasks[0].arguments == {"ticker": "AMD"}


@pytest.mark.parametrize("query", ["AMD表现如何？", "AMD股票表现怎么样？", "AMD今天成交量是不是低？", "AMD是不是放量上涨？", "AMD最近波动率多高？"])
def test_bare_stock_performance_defaults_to_recent_market_data(query):
    request = normalize_request(query)
    plan = RulePlanner().plan(request, route(request))
    assert plan.tasks[0].capability == "market.performance"


@pytest.mark.parametrize("query", ["AMD经营表现如何？", "AMD基本面表现如何？", "AMD最近财报表现如何？", "AMD技术面表现如何？"])
def test_non_price_performance_language_stays_with_research(query):
    request = normalize_request(query)
    plan = RulePlanner().plan(request, route(request))
    assert plan.tasks[0].capability == "research.stock"


def test_recent_earnings_quality_is_not_misrouted_as_price_performance():
    request = normalize_request("AMD最近的收益质量如何？")
    plan = RulePlanner().plan(request, route(request))
    assert plan.tasks[0].capability == "research.stock"


def test_move_explanation_cannot_be_overridden_by_the_llm_planner():
    llm = ScriptedLLM([LLMResponse(text='{"tasks":[{"id":"wrong","capability":"research.stock","arguments":{"ticker":"AMD"}}]}')])
    catalog = default_catalog()
    request = normalize_request("AMD今天为什么涨？")
    plan = StructuredLLMPlanner(llm, catalog).plan(request, route(request))
    assert plan.tasks[0].capability == "market.explain_move"
    assert not llm.calls


def test_market_observation_becomes_final_at_the_regular_close():
    before_close = _observation_state("2026-09-08", datetime(2026, 9, 8, 15, 59, tzinfo=ZoneInfo("America/New_York")))
    after_close = _observation_state("2026-09-08", datetime(2026, 9, 8, 16, 0, tzinfo=ZoneInfo("America/New_York")))
    assert before_close["is_intraday"] is True
    assert before_close["volume_is_final"] is False
    assert after_close["is_intraday"] is False
    assert after_close["volume_is_final"] is True


def test_catalog_exposes_only_requested_packs():
    catalog = default_catalog()
    research = catalog.names(["research"])
    assert "research.stock" in research
    assert "lab.backtest" not in research
    assert "web.research" not in research


def test_registry_blocks_unconfirmed_mutations():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    registry.register("state.mutate", lambda args, context: ToolEnvelope("state.mutate", ResultStatus.COMPLETED))
    result = registry.execute(PlanTask("write", "state.mutate", {"operation": "add", "payload": {}}), _context())
    assert not result.ok
    assert "confirmation" in result.errors[0]


def test_market_performance_adapter_returns_window_and_benchmark_evidence():
    class Prices:
        def get_prices(self, ticker, start, end):
            base = 100.0
            slope = 1.0 if ticker == "AMD" else 0.2
            start_day = date(2026, 7, 1)
            return [SimpleNamespace(time=(start_day + timedelta(days=index)).isoformat(), close=base + slope * index, volume=1_000_000 + index * 10_000) for index in range(35)]

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    now = datetime(2026, 8, 4, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    register_market_capabilities(registry, price_source_factory=Prices, move_provider=lambda ticker: None, now_factory=lambda: now)
    result = registry.execute(PlanTask("performance", "market.performance", {"ticker": "AMD"}), _context())
    assert result.ok
    assert result.metrics["returns"]["5d"] is not None
    assert result.metrics["relative_returns"]["SMH"]["5d"] is not None
    assert result.metrics["is_intraday"] is True
    assert {item.metadata["evidence_scope"] for item in result.evidence} >= {"price", "returns", "volume", "volatility", "benchmark"}
    answer = result.metadata["narrative"]
    assert "近 5 日回报" in answer
    assert "相对 SMH" in answer
    assert "盘中价格" in answer
    assert "不能据此判定是否放量或缩量" in answer
    assert verify_answer(answer, result.evidence, answer_mode=AnswerMode.TOOL_GROUNDED, results=[result]).ok


def test_market_move_adapter_splits_facts_and_causal_confidence():
    anomaly = SimpleNamespace(
        date="2026-09-08",
        price=508.71,
        price_change_pct=0.0652,
        volume_today=14_700_000,
        volume_avg_30d=22_900_000,
        volume_ratio=0.642,
        sector_etf="SMH",
        sector_return_1d=0.02,
        relative_1d_pp=0.0452,
        contrarian=False,
        reasons=[
            SimpleNamespace(text="公司发布直接利好", confidence="高", note="权威媒体同日报道"),
            SimpleNamespace(text="期权市场波动", confidence="低", note="缺少直接证据"),
        ],
        sources=[{"title": "Same-day report", "url": "https://example.test/report"}],
        next_steps=["观察 522 美元附近"],
        filtered_count=2,
    )
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    now = datetime(2026, 9, 8, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    register_market_capabilities(registry, price_source_factory=lambda: None, move_provider=lambda ticker: anomaly, now_factory=lambda: now)
    result = registry.execute(PlanTask("move", "market.explain_move", {"ticker": "AMD"}), _context())
    scopes = [item.metadata["evidence_scope"] for item in result.evidence]
    assert scopes[:3] == ["price", "volume", "benchmark"]
    assert result.metrics["confirmed_driver_count"] == 1
    assert result.findings[0]["confirmed"] is True
    assert result.findings[1]["confirmed"] is False
    candidate_evidence = next(item for item in result.evidence if item.metadata.get("claim_role") == "candidate_driver")
    assert candidate_evidence.confidence == 0.3
    assert candidate_evidence.metadata["driver_text"] == "期权市场波动"
    assert next(item for item in result.evidence if item.metadata.get("claim_role") == "attribution_assessment")
    assert result.metrics["is_intraday"] is True
    assert "盘中累计成交量" in next(item.claim for item in result.evidence if item.metadata.get("evidence_scope") == "volume")
    answer = result.metadata["narrative"]
    assert verify_answer(answer, result.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok


def _performance_envelope():
    class Prices:
        def get_prices(self, ticker, start, end):
            slope = 1.0 if ticker == "AMD" else 0.2
            first = date(2026, 7, 1)
            return [SimpleNamespace(time=(first + timedelta(days=index)).isoformat(), close=100.0 + slope * index, volume=1_000_000 + index * 10_000) for index in range(35)]

    registry = CapabilityRegistry(default_catalog())
    now = datetime(2026, 8, 4, 18, 0, tzinfo=ZoneInfo("America/New_York"))
    register_market_capabilities(registry, price_source_factory=Prices, move_provider=lambda ticker: None, now_factory=lambda: now)
    return registry.execute(PlanTask("performance", "market.performance", {"ticker": "AMD"}), _context())


def test_market_synthesis_falls_back_to_the_adapter_narrative_when_repair_fails():
    result = _performance_envelope()
    volatility = next(item for item in result.evidence if item.metadata["evidence_scope"] == "volatility")
    llm = ScriptedLLM([LLMResponse(text=f"AMD 波动率为 99%。[{volatility.id}]"), LLMResponse(text=f"AMD 波动率为 98%。[{volatility.id}]")])
    request = normalize_request("AMD最近表现如何？")
    plan = ExecutionPlan("AMD最近表现如何？", RouteKind.FAST_LOOKUP, tasks=(PlanTask("p", "market.performance", {"ticker": "AMD"}),), answer_mode=AnswerMode.TOOL_GROUNDED)
    answer = LLMEvidenceSynthesizer(llm).synthesize(request, plan, [result], result.evidence)
    assert "99%" not in answer and "98%" not in answer
    assert "近 5 日回报" in answer
    assert answer == result.metadata["narrative"]
    assert len(llm.calls) == 2


def test_generic_synthesizer_prefers_adapter_narratives_and_skips_uncitable_evidence():
    market = _performance_envelope()
    other = ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject="AMD", summary="AMD summary.", evidence=[EvidenceItem("E1", "AMD", "citable", metadata={}), EvidenceItem("E2", "AMD", "hidden", metadata={"citable": False})])
    answer = EvidenceSummarySynthesizer().synthesize(normalize_request("AMD"), ExecutionPlan("AMD", RouteKind.FAST_LOOKUP), [market, other], [*market.evidence, *other.evidence])
    assert answer.startswith(market.metadata["narrative"])
    assert "[E1]" in answer and "[E2]" not in answer


def test_executor_collects_structured_evidence():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def research(args, context):
        item = EvidenceItem("E1", args["ticker"], "supported claim")
        return ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject=args["ticker"], summary=item.claim, evidence=[item])

    registry.register("research.stock", research)
    plan = ExecutionPlan("research", RouteKind.RESEARCH, (PlanTask("one", "research.stock", {"ticker": "NVDA"}),), BudgetClass.STANDARD)
    outcome = ExecutionEngine(registry).run(plan, _context())
    results, ledger = outcome.results, outcome.ledger
    assert results[0].ok
    assert ledger.get("E1").entity == "NVDA"


def test_orchestrator_runs_end_to_end_with_an_injected_capability():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def research(args, context):
        item = EvidenceItem("E-NVDA", "NVDA", "NVDA evidence")
        return ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject="NVDA", summary="NVDA evidence", evidence=[item])

    registry.register("research.stock", research)
    result = AgentV2(catalog=catalog, registry=registry).run("分析 NVDA 的估值")
    assert result.status == RunStatus.COMPLETED
    assert result.verification.ok
    assert "[E-NVDA]" in result.answer


def test_command_waits_for_confirmation_without_dispatching():
    result = AgentV2().run("把 NVDA 加入关注列表")
    assert result.status == RunStatus.WAITING_CONFIRMATION
    assert not result.results


def test_research_adapter_preserves_engine_evidence():
    class FakeEngine:
        def run(self, ticker, modules=None):
            return {
                "ticker": ticker,
                "run_id": "research-1",
                "status": "COMPLETED",
                "generated_at": "2026-09-07T00:00:00Z",
                "core_thesis": "Evidence-backed thesis",
                "scores": {"valuation": 60},
                "risk_level": "Medium",
                "research_confidence": {"score": 80},
                "research_findings": [{"claim": "Revenue grew", "evidence_ids": ["ev-1"]}],
                "evidence_index": [{"id": "ev-1", "ticker": ticker, "module": "fundamental", "claim": "Revenue grew", "metrics": {"revenue_growth": 0.1}, "source_ids": ["fd_metrics"], "verified": True}],
                "sources": [{"id": "fd_metrics", "title": "Metrics", "url": "https://example.test", "published_at": "2026-09-01"}],
                "confidence_limitations": ["expectations missing forward estimates"],
                "production_diagnostics": {"modules": {"expectations": {"status": "PARTIAL", "completeness": 0.25, "missing_fields": ["forward_eps"]}}},
            }

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    register_research_capabilities(registry, engine_factory=FakeEngine)
    result = registry.execute(PlanTask("r", "research.stock", {"ticker": "NVDA", "focus": "fundamentals"}), _context())
    assert result.ok
    assert result.evidence[0].id == "ev-1"
    assert result.evidence[0].source_url == "https://example.test"
    metrics_evidence = next(item for item in result.evidence if item.metadata.get("citation_kind") == "metrics")
    assert metrics_evidence.id.startswith("evidence-research-metrics-")
    assert metrics_evidence.metadata["metrics"]["scores"]["valuation"] == 60
    limitation_evidence = next(item for item in result.evidence if item.metadata.get("citation_kind") == "limitations")
    assert "expectations 数据完整度 25.0%" in limitation_evidence.claim
    assert limitation_evidence.metadata["module_diagnostics"]["expectations"]["completeness"] == 0.25


def test_research_adapter_disambiguates_conflicting_ids_from_cached_results():
    class CachedEngine:
        def run(self, ticker, modules=None):
            return {
                "ticker": ticker,
                "run_id": "cached-research",
                "status": "COMPLETED",
                "generated_at": "2026-09-08T00:00:00Z",
                "core_thesis": "Cached thesis",
                "research_findings": [],
                "evidence_index": [
                    {"id": "legacy-id", "ticker": ticker, "module": "sec", "claim": "First filing excerpt.", "source_ids": ["sec_filings"]},
                    {"id": "legacy-id", "ticker": ticker, "module": "sec", "claim": "Second filing excerpt.", "source_ids": ["sec_filings"]},
                ],
                "sources": [{"id": "sec_filings", "title": "SEC filing", "url": "https://example.test/filing"}],
                "production_diagnostics": {"modules": {}},
            }

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    register_research_capabilities(registry, engine_factory=CachedEngine)
    plan = ExecutionPlan("research", RouteKind.RESEARCH, (PlanTask("one", "research.stock", {"ticker": "NVDA"}),), BudgetClass.STANDARD)
    outcome = ExecutionEngine(registry).run(plan, _context())
    results, ledger = outcome.results, outcome.ledger
    assert results[0].ok
    assert len(ledger.items()) == 2
    assert len(ledger.ids()) == 2
    repaired = next(item for item in ledger.items() if item.id != "legacy-id")
    assert repaired.metadata["original_evidence_id"] == "legacy-id"
    assert repaired.metadata["collision_disambiguated"] is True


def test_evidence_conflict_is_not_reported_as_a_valid_plan_or_verification():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def conflicting_research(args, context):
        return ToolEnvelope(
            "research.stock",
            ResultStatus.COMPLETED,
            subject=args["ticker"],
            evidence=[
                EvidenceItem("same-id", args["ticker"], "First claim"),
                EvidenceItem("same-id", args["ticker"], "Second claim"),
            ],
        )

    registry.register("research.stock", conflicting_research)
    result = AgentV2(catalog=catalog, registry=registry).run("分析 NVDA")
    # A different claim under a reused id is reissued, disclosed, and kept citeable; the run completes.
    assert result.status == RunStatus.COMPLETED, (result.status, result.error)
    ids = {item.id for item in result.evidence}
    assert "same-id" in ids and any(value.startswith("same-id~") for value in ids)
    assert {item.claim for item in result.evidence} == {"First claim", "Second claim"}
    assert any("重新编号" in value for value in result.results[0].limitations)
    assert result.verification.ok


def test_web_facade_returns_transport_neutral_dict():
    payload = WebFacade(AgentV2()).handle(WebRequest("什么是市盈率？", session_id="web-1"))
    assert payload["request"]["session_id"] == "web-1"
    assert payload["route"]["kind"] == "general_knowledge"


def test_telegram_facade_depends_only_on_a_transport_protocol():
    class Transport:
        def __init__(self):
            self.events = []

        async def typing(self, chat_id):
            self.events.append("typing")

        async def progress(self, chat_id, event):
            self.events.append(event.status.value)

        async def deliver(self, chat_id, result):
            self.events.append("delivered")

    async def scenario():
        transport = Transport()
        result = await TelegramFacade(AgentV2()).handle(TelegramMessage(7, "什么是 ROE？"), transport)
        return transport, result

    transport, result = asyncio.run(scenario())
    assert transport.events[0] == "typing"
    assert transport.events[-1] == "delivered"
    assert result.request.session_id == "7"


def test_llm_planner_accepts_only_declared_capabilities():
    response = LLMResponse(text='{"objective":"研究估值","tasks":[{"id":"t1","capability":"research.stock","arguments":{"ticker":"NVDA","focus":"valuation"}}]}')
    catalog = default_catalog()
    planner = StructuredLLMPlanner(ScriptedLLM([response]), catalog)
    request = normalize_request("分析 NVDA 的估值")
    plan = planner.plan(request, route(request))
    assert plan.tasks[0].capability == "research.stock"
    assert plan.tasks[0].arguments["focus"] == "valuation"


def test_llm_planner_falls_back_when_model_invents_a_capability():
    response = LLMResponse(text='{"tasks":[{"id":"t1","capability":"trade.execute","arguments":{}}]}')
    catalog = default_catalog()
    planner = StructuredLLMPlanner(ScriptedLLM([response]), catalog)
    request = normalize_request("分析 NVDA 的风险")
    plan = planner.plan(request, route(request))
    assert plan.tasks[0].capability == "research.stock"
    assert any("fallback" in value for value in plan.assumptions)


def test_llm_planner_preserves_explicit_lab_parameters():
    response = LLMResponse(text='{"tasks":[{"id":"lab","capability":"lab.backtest","arguments":{"strategy":"momentum","tickers":["NVDA"],"holding_days":21,"cost_bps":10}}]}')
    catalog = default_catalog()
    planner = StructuredLLMPlanner(ScriptedLLM([response]), catalog)
    request = normalize_request("回测 NVDA 动量策略，持有21天，成本10bp")
    plan = planner.plan(request, route(request))
    assert plan.tasks[0].capability == "lab.backtest"
    assert plan.tasks[0].arguments["holding_days"] == 21
    assert plan.tasks[0].arguments["cost_bps"] == 10
    assert plan.budget == BudgetClass.LAB


def test_llm_synthesizer_requires_evidence_ids_in_its_prompt_contract():
    llm = ScriptedLLM([LLMResponse(text="结论有证据支持。[E1]")])
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request("分析 NVDA")
    plan = ExecutionPlan("分析 NVDA", RouteKind.RESEARCH)
    evidence = [EvidenceItem("E1", "NVDA", "支持结论")]
    answer = synthesizer.synthesize(request, plan, [], evidence)
    assert answer.endswith("[E1]")
    system = llm.calls[0][0]["content"]
    payload = json.loads(llm.calls[0][1]["content"])
    assert "3—5 个短段落" in system
    assert payload["response_style"] == "brief"
    assert payload["response_intent"] == "stock_research"


def test_llm_synthesizer_only_requests_detailed_style_when_user_asks_for_it():
    llm = ScriptedLLM([LLMResponse(text="详细结论。[E1]")])
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request("给我一份 NVDA 的完整详细报告")
    plan = ExecutionPlan("详细分析 NVDA", RouteKind.RESEARCH)
    evidence = [EvidenceItem("E1", "NVDA", "支持结论")]
    synthesizer.synthesize(request, plan, [], evidence)
    payload = json.loads(llm.calls[0][1]["content"])
    assert payload["response_style"] == "detailed"


@pytest.mark.parametrize(
    ("capability", "intent", "guidance"),
    [("market.performance", "recent_performance", "recent_performance："), ("market.explain_move", "move_explanation", "move_explanation："), ("research.stock", "stock_research", "stock_research：")],
)
def test_llm_synthesizer_derives_intent_and_guidance_from_the_capabilities_used(capability, intent, guidance):
    llm = ScriptedLLM([LLMResponse(text="有证据的回答。[E1]")])
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request("AMD 怎么样")
    evidence = [EvidenceItem("E1", "AMD", "支持结论")]
    plan = ExecutionPlan("AMD 怎么样", RouteKind.RESEARCH, tasks=(PlanTask("t", capability, {"ticker": "AMD"}),))
    synthesizer.synthesize(request, plan, [], evidence)
    payload = json.loads(llm.calls[0][1]["content"])
    assert payload["response_intent"] == intent
    system = llm.calls[0][0]["content"]
    assert guidance in system
    assert "盘中" not in system or capability.startswith("market.")


def test_llm_synthesizer_normalizes_valid_result_paths_to_evidence_ids():
    llm = ScriptedLLM(
        [
            LLMResponse(
                text=(
                    "基本面评分为 96/100。[results.metrics.scores.fundamental] "
                    "预期数据不足。[results.limitations]"
                )
            )
        ]
    )
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request("分析 NVDA")
    plan = ExecutionPlan("分析 NVDA", RouteKind.RESEARCH)
    result = ToolEnvelope(
        "research.stock",
        ResultStatus.COMPLETED,
        metrics={"scores": {"fundamental": 96}},
        limitations=["expectations: PARTIAL_DATA"],
        run_id="research-1",
    )
    evidence = [
        EvidenceItem(
            "evidence-research-metrics-1",
            "NVDA",
            "NVDA fundamental score is 96/100.",
            producer_run_id="research-1",
            metadata={"citation_kind": "metrics"},
        ),
        EvidenceItem(
            "evidence-research-limitations-1",
            "NVDA",
            "NVDA expectations data is incomplete.",
            producer_run_id="research-1",
            metadata={"citation_kind": "limitations"},
        ),
    ]
    answer = synthesizer.synthesize(request, plan, [result], evidence)
    assert "[results." not in answer
    assert "[evidence-research-metrics-1]" in answer
    assert "[evidence-research-limitations-1]" in answer


def test_llm_synthesizer_repairs_an_invalid_result_path_instead_of_shipping_it():
    llm = ScriptedLLM(
        [
            LLMResponse(text="虚构评分为 96。[results.metrics.scores.invented]"),
            LLMResponse(text="基本面评分为 96。[evidence-research-metrics-1]"),
        ]
    )
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request("分析 NVDA")
    plan = ExecutionPlan("分析 NVDA", RouteKind.RESEARCH, answer_mode=AnswerMode.RESEARCH_GROUNDED)
    result = ToolEnvelope(
        "research.stock",
        ResultStatus.COMPLETED,
        metrics={"scores": {"fundamental": 96}},
        run_id="research-1",
    )
    evidence = [
        EvidenceItem(
            "evidence-research-metrics-1",
            "NVDA",
            "NVDA fundamental score is 96/100.",
            producer_run_id="research-1",
            metadata={"citation_kind": "metrics"},
        )
    ]
    answer = synthesizer.synthesize(request, plan, [result], evidence)
    assert answer == "基本面评分为 96。[evidence-research-metrics-1]"
    assert len(llm.calls) == 2
    assert "results.metrics.scores.invented" in llm.calls[1][3]["content"]
    assert verify_answer(answer, evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok


def test_verifier_rejects_an_invented_number_even_with_a_valid_citation():
    evidence = [EvidenceItem("E1", "NVDA", "Revenue growth was 10%.")]
    report = verify_answer(
        "NVDA 收入增长 20%。[E1]",
        evidence,
        answer_mode=AnswerMode.RESEARCH_GROUNDED,
    )
    assert not report.ok
    assert "20" in report.ungrounded_numbers


def test_verifier_grounds_result_metrics_and_scaled_evidence_values():
    evidence = [
        EvidenceItem("evidence-999999", "NVDA", "Insider net transaction value was -349029376."),
        EvidenceItem("evidence-metrics", "NVDA", "Score 87/100 and completeness 0.857."),
    ]
    result = ToolEnvelope(
        "research.stock",
        ResultStatus.COMPLETED,
        metrics={"score": 87, "max_score": 100, "completeness": 0.857},
        findings=[{"claim": "Insiders were net sellers", "evidence_ids": ["evidence-999999"]}],
        evidence=evidence,
    )
    report = verify_answer(
        "综合评分 87/100，数据完整性 85.7%。[evidence-metrics] 内部人净卖出约 -3.49 亿美元。[evidence-999999]",
        evidence,
        answer_mode=AnswerMode.RESEARCH_GROUNDED,
        results=[result],
    )
    assert report.ok
    assert not report.ungrounded_numbers


def test_verifier_requires_nearby_citation_to_support_nearby_number():
    evidence = [
        EvidenceItem("E-REVENUE", "NVDA", "Revenue growth was 10%."),
        EvidenceItem("E-MARGIN", "NVDA", "Gross margin was 20%."),
    ]
    report = verify_answer(
        "NVDA 收入增长 20%。[E-REVENUE]",
        evidence,
        answer_mode=AnswerMode.RESEARCH_GROUNDED,
    )
    assert not report.ok
    assert any("邻近数字" in warning for warning in report.warnings)


def test_verifier_enforces_evidence_declared_forbid_unless_rules():
    rule = {"forbid": r"主要原因", "unless": r"可能", "warning": "候选归因被表述为已确认原因"}
    evidence = [EvidenceItem("C1", "AMD", "低置信度候选解释：期权市场波动。", metadata={"constraints": [rule]})]
    rejected = verify_answer("AMD 上涨的主要原因是期权市场波动。[C1]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED)
    assert not rejected.ok
    assert any("候选归因" in warning for warning in rejected.warnings)
    hedged = verify_answer("AMD 上涨的主要原因可能是期权市场波动。[C1]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED)
    assert hedged.ok


def test_verifier_enforces_evidence_declared_require_rules():
    rule = {"require": r"盘中|截至查询时", "warning": "盘中价格被表述为完整收盘口径"}
    evidence = [EvidenceItem("P1", "AMD", "Intraday price.", metadata={"constraints": [rule]})]
    assert not verify_answer("AMD 收盘价走强。[P1]", evidence, answer_mode=AnswerMode.TOOL_GROUNDED).ok
    assert verify_answer("AMD 盘中价格走强。[P1]", evidence, answer_mode=AnswerMode.TOOL_GROUNDED).ok


def test_verifier_enforces_result_level_caps_and_forbidden_phrases():
    evidence = [
        EvidenceItem("C1", "AMD", "Candidate one.", metadata={"claim_role": "candidate_driver"}),
        EvidenceItem("C2", "AMD", "Candidate two.", metadata={"claim_role": "candidate_driver"}),
    ]
    result = ToolEnvelope(
        "market.explain_move",
        ResultStatus.COMPLETED,
        subject="AMD",
        evidence=evidence,
        metadata={"answer_constraints": [{"max_cited": {"metadata": {"claim_role": "candidate_driver"}, "max": 1, "warning": "未确认直接驱动时展示了过多弱候选线索"}}, {"forbid": r"0\s*个", "warning": "将内部归因计数直接暴露给用户"}]},
    )
    report = verify_answer("可能与线索一相关。[C1] 也可能与线索二相关。[C2]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result])
    assert any("过多弱候选" in warning for warning in report.warnings)
    report = verify_answer("有 0 个驱动。[C1]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result])
    assert any("归因计数" in warning for warning in report.warnings)
    assert verify_answer("可能与线索一相关。[C1]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok


def test_verifier_rejects_citations_of_uncitable_evidence_and_uncited_market_figures():
    evidence = [EvidenceItem("C1", "AMD", "Candidate one.", metadata={"citable": False, "uncitable_warning": "展示了缺乏直接支持的过弱异动线索"}), EvidenceItem("P1", "AMD", "AMD close 100.00.")]
    result = ToolEnvelope("market.explain_move", ResultStatus.COMPLETED, subject="AMD", evidence=evidence, metadata={"require_cited_numbers": True})
    report = verify_answer("可能与该线索相关。[C1]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result])
    assert any("过弱异动线索" in warning for warning in report.warnings)
    report = verify_answer("AMD 收于 100.00 美元。 详情见证据。[P1]", evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result])
    assert any("邻近引用" in warning for warning in report.warnings)


def test_verifier_does_not_treat_digits_in_opaque_ids_as_observations():
    evidence = [EvidenceItem("evidence-999999", "NVDA", "Insiders were net sellers.")]
    result = ToolEnvelope(
        "research.stock",
        ResultStatus.COMPLETED,
        findings=[{"claim": "Insiders were net sellers", "evidence_ids": ["evidence-999999"]}],
        evidence=evidence,
    )
    report = verify_answer(
        "NVDA 的指标值是 999999。[evidence-999999]",
        evidence,
        answer_mode=AnswerMode.RESEARCH_GROUNDED,
        results=[result],
    )
    assert not report.ok
    assert "999999" in report.ungrounded_numbers


def test_web_and_lab_ports_can_be_injected_without_core_dependencies():
    class Search:
        def search(self, query, **kwargs):
            return ToolEnvelope(
                "web.research",
                ResultStatus.COMPLETED,
                summary="web result",
                evidence=[EvidenceItem("WEB1", "", "web result")],
            )

    class Lab:
        def run(self, capability, arguments, context):
            return ToolEnvelope(capability, ResultStatus.COMPLETED, summary="lab result")

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    register_web_capability(registry, Search())
    register_lab_capabilities(registry, Lab())
    web_context = ExecutionContext(
        "web",
        NormalizedRequest("q", "q", allow_web=True),
        BudgetClass.STANDARD,
        allow_web=True,
    )
    web = registry.execute(
        PlanTask("w", "web.research", {"query": "q", "topic": "company_event"}),
        web_context,
    )
    lab = registry.execute(PlanTask("l", "lab.backtest", {"strategy": "momentum"}), _context())
    assert web.ok and web.evidence[0].id == "WEB1"
    assert lab.ok


def test_short_term_session_resolves_a_follow_up_before_routing():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def research(args, context):
        ticker = args["ticker"]
        item = EvidenceItem(f"E-{ticker}", ticker, f"{ticker} evidence")
        return ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject=ticker, summary=item.claim, evidence=[item])

    registry.register("research.stock", research)
    memory = ShortTermSession()
    agent = AgentV2(catalog=catalog, registry=registry, session=memory)
    first = agent.run("分析 NVDA 的风险", session_id="chat-1")
    second = agent.run("那它财报呢？", session_id="chat-1")
    assert first.status == RunStatus.COMPLETED
    assert second.request.entities == ("NVDA",)
    assert second.request.metadata["rewritten"]
    assert second.to_dict()["request"]["metadata"]["antecedent"] == "NVDA"


def test_short_term_session_carries_the_stock_an_answer_named_into_a_subjectless_follow_up():
    memory = ShortTermSession()
    agent = AgentV2(catalog=default_catalog(), registry=_framed_registry(), session=memory)
    first = agent.run("我的仓库里哪只跌的最多?", session_id="chat-2")
    assert first.request.entities == () and first.answer.startswith("按买入以来的浮动盈亏排序，最低的是 ARM")
    third = agent.run("它财报怎么样", session_id="chat-2")  # the pronoun path now finds the focus too
    assert third.request.text == "ARM财报怎么样" and third.results[0].subject == "ARM"
    assert third.request.metadata["context_frame"]["ticker"] == "ARM"
    # Questions that name their own scope or stock are left alone.
    for query in ("宏观怎么样", "我的持仓风险怎么样", "NVDA 为什么跌", "把 HPE 加到关注列表"):
        assert not memory.resolve("chat-2", query).rewritten, query
    assert not memory.resolve("chat-fresh", "什么原因跌这么多?").rewritten  # nothing to refer back to


def test_a_why_follow_up_after_a_loss_ranking_explains_the_loss_since_purchase_not_today():
    memory = ShortTermSession()
    agent = AgentV2(catalog=default_catalog(), registry=_framed_registry(), session=memory)
    agent.run("我的仓库里哪只跌的最多?", session_id="chat-3")
    resolution = memory.resolve("chat-3", "什么原因跌这么多?")
    assert resolution.frame["ticker"] == "ARM" and resolution.frame["field"] == "pl_pct" and resolution.frame["value"] == -32.22
    second = agent.run("什么原因跌这么多?", session_id="chat-3")
    assert second.request.text == "ARM 什么原因跌这么多?"
    assert [task.capability for task in second.plan.tasks] == ["account.portfolio", "market.performance", "market.drawdown", "filings.recent", "market.anomaly_history", "market.attribute_move"]
    assert second.plan.tasks[2].arguments == {"ticker": "ARM", "loss_pct": -32.22, "top": 3}
    attributor = second.plan.tasks[5]
    assert attributor.fan_out == {"from": "market-drawdown", "field": "worst_dates", "argument": "date", "max": 3} and not attributor.required
    assert [result.subject for result in second.results if result.capability == "market.attribute_move"] == ["ARM"]
    assert "最相关的一条候选线索是“财报指引低于预期”，只能作为排查方向[AT-ARM-2026-08-05-filing]。" in second.answer
    assert second.plan.assumptions[0].startswith("context_frame: 用户追问的是 ARM 买入以来的浮动盈亏 -32.22%（成本价 $389.52）")
    lines = second.answer.split("\n")
    assert lines[0].startswith("你问的是 ARM 买入以来的浮动盈亏：-32.22%，成本价 $389.52[legacy-")
    assert lines[1] == "今日盘中为上涨（+0.94%），与买入以来的浮动盈亏是不同区间[P-ARM]。"
    assert lines[2] == "对照区间回报（近 5 日 +12.32%、近 1 月 -1.53%、近 3 月 -21.40%、近 1 年 +89.85%），这段跌幅大部分落在近 3 月内[W-ARM]。"
    assert lines[3] == "近 1 年 +89.85% 而该持仓仍在浮亏，说明买入点在这轮上涨之后的高位[W-ARM]。"
    assert "组合价值" not in second.answer and "近 1 月回报 -1.53%" not in second.answer  # the narrative's returns line is not repeated
    assert "相对 SMH，单日超额 +0.94%[B-ARM]。" in second.answer and "成交量尚未定型" not in second.answer
    assert "ARM 期间跌幅最大的交易日：2026-08-05 -13.21%[D-ARM-0805]。" in second.answer
    # The window return was already in the lead; the stretch block does not repeat it.
    assert "区间回报 -21.40%" not in second.answer.split("\n\n", 1)[1]
    assert "2026-03-01" not in second.answer  # a filing before the decline window is left out
    # The 08-05 watch record is superseded by that day's attribution block; the
    # filings are listed on one line because the attributor read them.
    assert "盯盘记录" not in second.answer and "ARM 这段下跌期间 1 份申报：2026-08-05 8-K[F-ARM-0805]。" in second.answer
    # Reading order is fixed: the stretch, then the filings, then the day blocks.
    positions = [second.answer.index(marker) for marker in ("从 2026-06-18 的高点", "这段下跌期间 1 份申报", "ARM 在 2026-08-05 收于")]
    assert positions == sorted(positions)
    from v2.agent_v2.synthesis import anomaly_lines, filing_line

    history = ToolEnvelope("market.anomaly_history", ResultStatus.COMPLETED, subject="ARM", evidence=[
        EvidenceItem(f"A-{day}", "ARM", f"ARM {day} 盯盘记录：{flags}；note。", metadata={"evidence_scope": "anomaly", "date": day, "flags": flags})
        for day, flags in (("2026-09-09", ""), ("2026-07-24", "retro_attribution"), ("2026-07-10", "volume_spike"), ("2026-06-23", "gap_down"), ("2026-06-01", "gap_down"))
    ])
    # Inside peak→trough only, minus attributed days and retro memories: 07-10 survives.
    assert anomaly_lines(history, "2026-06-18", "2026-07-29", frozenset({"2026-06-23"})) == "- ARM 2026-07-10 盯盘记录：volume_spike；note。 [A-2026-07-10]"
    filings = ToolEnvelope("filings.recent", ResultStatus.COMPLETED, subject="ARM", evidence=[
        EvidenceItem(f"F-{day}", "ARM", f"ARM 于 {day} 向 SEC 提交了 6-K（x）。", metadata={"evidence_scope": "filing", "date": day, "form": "6-K"})
        for day in ("2026-08-10", "2026-08-01", "2026-07-29")
    ])
    # Three days after the trough is the attributor's own reading margin; 08-10 is out.
    assert filing_line(filings, "2026-06-18", "2026-08-01") == "ARM 这段下跌期间 2 份申报：2026-08-01 6-K[F-2026-08-01]；2026-07-29 6-K[F-2026-07-29]。"
    assert "AT-ARM-2026-08-05-news" not in second.answer  # no web consent: the attributor had no news to cite
    # With web consent the same plan lets the attributor use the news.
    consenting = AgentV2(catalog=default_catalog(), registry=_framed_registry(), session=memory, config=AgentV2Config(enable_web_fallback=True))
    consenting.run("我的仓库里哪只跌的最多?", session_id="chat-4")
    with_web = consenting.run("为什么跌这么多?", session_id="chat-4", allow_web=True)
    assert [task.capability for task in with_web.plan.tasks][-1] == "market.attribute_move"
    assert "能直接支持的高置信度驱动：财报后指引令市场失望，股价大跌[AT-ARM-2026-08-05-news]。" in with_web.answer
    assert with_web.verification.ok
    assert second.verification.ok, second.verification
    assert second.status == RunStatus.COMPLETED
    # The frame survives the framed turn, and a question about a rise is not a drawdown question.
    assert memory.resolve("chat-3", "为什么涨").frame["ticker"] == "ARM"
    plan = RulePlanner().plan(normalize_request("ARM 为什么涨", metadata={"context_frame": resolution.frame}), route(normalize_request("ARM 为什么涨")))
    assert [task.capability for task in plan.tasks] == ["market.explain_move"]
    # The LLM planner leaves the framed plan to the rules.
    llm = ScriptedLLM([LLMResponse(text="{}")])
    framed = normalize_request("ARM 什么原因跌这么多?", metadata={"context_frame": resolution.frame})
    assert len(StructuredLLMPlanner(llm, default_catalog()).plan(framed, route(framed)).tasks) == 6 and llm.calls == []


_FILING_TEXT = """UNITED STATES SECURITIES AND EXCHANGE COMMISSION
FORM 8-K
Item 2.02 Results of Operations and Financial Condition
On July 29, 2026, Arm Holdings plc announced results for the quarter. Revenue of $1.05 billion was below the guidance range; the company now expects fiscal-year revenue growth in the low twenties.
Item 5.02 Departure of Directors or Certain Officers
On July 28, 2026, the Chief Financial Officer notified the board of his intention to resign effective September 1, 2026.
Item 9.01 Financial Statements and Exhibits
Exhibit 99.1 Press release dated July 29, 2026.
"""


class _FakeFilingSource:
    def __init__(self, refs):
        self.refs = refs
        self.reads: list[tuple[str, str]] = []

    def list_filings(self, ticker, since, until):
        return [ref for ref in self.refs if since <= ref.filing_date <= until]

    def outline(self, ref):
        from v2.agent_v2.agents.filing_reader import Section, sections_of

        return [Section(section_id, title, len(body)) for section_id, title, body in sections_of(_FILING_TEXT, ref.form)]

    def read(self, ref, section_id):
        from v2.agent_v2.agents.filing_reader import sections_of

        self.reads.append((ref.accession, section_id))
        return next((body for candidate, _, body in sections_of(_FILING_TEXT, ref.form) if candidate == section_id), "")


def test_edgar_source_appends_a_6k_exhibit_so_the_reader_can_choose_it():
    from v2.agent_v2.agents.filing_reader import EdgarFilingSource

    class Attachment:
        def __init__(self, kind, description, body):
            self.document_type, self.description, self._body = kind, description, body

        def text(self):
            return self._body

    class Raw:
        accession_number = "0001-26-000900"
        filing_date = "2026-07-29"
        form = "6-K"
        cik = "0001973239"
        homepage_url = "https://www.sec.gov/x/900/"
        attachments = [
            Attachment("6-K", "cover", "FORM 6-K Report of foreign private issuer"),
            Attachment("EX-99.1", "Press release", "<html><body><p>Arm Holdings plc reports results for the first quarter. Revenue was $1.05 billion, below the guidance range of $1.10 to $1.20 billion.</p></body></html>"),
            Attachment("EX-99.2", "Shareholder letter", "Second   exhibit   text."),
        ]

        def text(self):
            return "FORM 6-K\nReport of foreign private issuer pursuant to Rule 13a-16.\nArm Holdings plc furnishes the exhibits listed herein."

    source = EdgarFilingSource(fetch=lambda ticker, form, since, until: [Raw()] if form == "6-K" else [])
    refs = source.list_filings("ARM", "2026-07-15", "2026-08-01")
    assert [ref.form for ref in refs] == ["6-K"] and refs[0].url == "https://www.sec.gov/x/900/"
    outline = source.outline(refs[0])
    assert [section.title for section in outline][1:] == ["EXHIBIT 99.1 Press release", "EXHIBIT 99.2 Shareholder letter"]
    body = source.read(refs[0], outline[1].id)
    assert "Revenue was $1.05 billion" in body and "<p>" not in body
    assert source.read(refs[0], outline[2].id).endswith("Second exhibit text.")


def test_filing_reader_reads_the_sections_it_chooses_and_keeps_only_quoted_events():
    from v2.agent_v2.agents.filing_reader import FilingReader, FilingRef, sections_of

    parts = sections_of(_FILING_TEXT, "8-K")
    assert [part[0] for part in parts] == ["s0", "s1", "s2", "s3"] and parts[1][1].startswith("Item 2.02")
    assert parts[0][1] == "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"  # the cover page stays readable
    assert [part[0] for part in sections_of("x" * 8000, "6-K")] == ["part-1", "part-2", "part-3"]
    # A long exhibit whose table headers all look like headings collapses to at most twenty sections.
    noisy = "\n".join(f"REVENUE BY SEGMENT TABLE {index}\n" + ("row of figures " * 20) for index in range(150))
    merged = sections_of(noisy, "6-K")
    assert 15 <= len(merged) <= 20 and merged[0][1].startswith("REVENUE BY SEGMENT TABLE 0 …（含后续")
    assert sum(len(body) for _, _, body in merged) >= len(noisy) - 150 * 2  # nothing is dropped, only joined
    paged = sections_of("y" * 100_000, "6-K")
    assert len(paged) == 20 and sum(len(body) for _, _, body in paged) == 100_000
    refs = [FilingRef("ARM", "8-K", "2026-07-29", "0001-26-000777", "https://www.sec.gov/x/777/"), FilingRef("ARM", "8-K", "2026-05-02", "0001-26-000500", "https://www.sec.gov/x/500/")]
    source = _FakeFilingSource(refs)
    llm = ScriptedLLM(
        [
            LLMResponse(text='{"action":"read","filing":1,"section":"s1"}'),  # the single-read form still works
            LLMResponse(text='```json\n{"action":"read","reads":[{"filing":1,"section":"s2"},{"filing":9,"section":"s1"}]}\n```'),
            LLMResponse(
                text=json.dumps(
                    {
                        "action": "finish",
                        "events": [
                            {"date": "2026-07-29", "summary": "季度营收 10.5 亿美元低于指引区间", "quote": "Revenue of $1.05 billion was below the guidance range", "filing": 1, "section": "s1"},
                            {"date": "2026-07-28", "summary": "CFO 提出辞职", "quote": "the Chief Financial Officer notified the board of his intention to resign", "filing": 1, "section": "s2"},
                            {"date": "2026-07-29", "summary": "指引下调（模型改写了标点和数字）", "quote": "the company now expects fiscal year revenue growth in the low-twenties", "filing": 1, "section": "s1"},
                            {"date": "2026-07-29", "summary": "编造的事件", "quote": "the company was acquired", "filing": 1, "section": "s1"},
                        ],
                        "note": "两节都读完了",
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )
    reader = FilingReader(llm, source, max_rounds=4)
    result = reader.run("ARM", _context(), around="2026-07-29", today=date(2026, 9, 9))
    assert result.ok and result.status == ResultStatus.COMPLETED
    assert source.reads == [("0001-26-000777", "s1"), ("0001-26-000777", "s2")]  # the May filing is outside the ±14-day window
    events = [item for item in result.evidence if item.metadata.get("evidence_scope") == "filing_event"]
    assert [item.metadata["date"] for item in events] == ["2026-07-29", "2026-07-28", "2026-07-29"]
    assert events[0].source_url == "https://www.sec.gov/x/777/" and "Revenue of $1.05 billion" in events[0].claim
    # A quote the model reworded is replaced by the filing's own words around the matching run.
    assert events[2].metadata["quote"] == "the company now expects fiscal-year revenue growth in the low twenties."
    assert {key: result.metrics[key] for key in ("filings", "sections_read", "events", "rounds", "llm_calls", "stop_reason")} == {"filings": 1, "sections_read": 2, "events": 3, "rounds": 3, "llm_calls": 3, "stop_reason": "finished"}
    assert "1 条事件的引文与已读文本不符，已丢弃" in result.limitations[0]
    assert result.metadata["narrative"].startswith("ARM 申报中读到的事件：2026-07-29 季度营收 10.5 亿美元低于指引区间[evidence-filing-event-")
    assert verify_answer(result.metadata["narrative"], result.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok
    # A reader that keeps reading gets one forced finish so what it read is not wasted.
    forced = ScriptedLLM(
        [
            LLMResponse(text='{"action":"read","filing":1,"section":"s1"}'),
            LLMResponse(text='{"action":"read","filing":1,"section":"s2"}'),
            LLMResponse(text=json.dumps({"action": "finish", "events": [{"date": "2026-07-29", "summary": "营收低于指引", "quote": "Revenue of $1.05 billion was below the guidance range", "filing": 1, "section": "s1"}], "note": "被要求结束"}, ensure_ascii=False)),
        ]
    )
    rescued = FilingReader(forced, _FakeFilingSource(refs), max_rounds=2).run("ARM", _context(), around="2026-07-29", today=date(2026, 9, 9))
    assert rescued.status == ResultStatus.COMPLETED and {key: rescued.metrics[key] for key in ("filings", "sections_read", "events", "rounds", "llm_calls")} == {"filings": 1, "sections_read": 2, "events": 1, "rounds": 2, "llm_calls": 3}
    assert forced.calls[-1][-1]["content"].startswith("轮次已用完")
    # If even the forced finish keeps reading, the cap holds and only a limitation comes back.
    endless = FilingReader(ScriptedLLM([LLMResponse(text='{"action":"read","filing":1,"section":"s1"}')] * 6), _FakeFilingSource(refs), max_rounds=2)
    capped = endless.run("ARM", _context(), around="2026-07-29", today=date(2026, 9, 9))
    assert capped.status == ResultStatus.PARTIAL_DATA and {key: capped.metrics[key] for key in ("filings", "sections_read", "events", "rounds", "llm_calls", "stop_reason")} == {"filings": 1, "sections_read": 1, "events": 0, "rounds": 2, "llm_calls": 3, "stop_reason": "rounds"} and "达到轮次上限" in capped.limitations[0]
    assert capped.evidence[0].metadata["citation_kind"] == "limitations" and "未读到与2026-07-29 附近下跌相关的事件" in capped.evidence[0].claim
    # Without a model the capability still lists the filings and says it did not read them.
    listed = FilingReader(None, _FakeFilingSource(refs)).run("ARM", _context(), around="2026-07-29", today=date(2026, 9, 9))
    assert listed.status == ResultStatus.PARTIAL_DATA and listed.metadata["filings"][0]["accession"] == "0001-26-000777" and "未配置模型" in listed.limitations[0]
    nothing = FilingReader(llm, _FakeFilingSource([])).run("ARM", _context(), around="2026-07-29", today=date(2026, 9, 9))
    assert nothing.ok and "未查到申报" in nothing.evidence[0].claim


def test_market_drawdown_locates_the_worst_days_and_the_peak_to_trough():
    class Prices:
        def get_prices(self, ticker, start, end):
            first = date(2026, 1, 5)
            rows = []
            close = 300.0
            for index in range(180):
                day = first + timedelta(days=index)
                if day.weekday() >= 5:
                    continue
                if day == date(2026, 5, 20):
                    close *= 0.80  # the crash day
                elif day == date(2026, 6, 3):
                    close *= 0.95
                elif day < date(2026, 5, 20):
                    close *= 1.002
                else:
                    close *= 0.999
                if day.isoformat() <= str(end):
                    rows.append(SimpleNamespace(time=day.isoformat(), close=round(close, 2), volume=1_000_000))
            return rows

    class SectorPrices(Prices):
        def get_prices(self, ticker, start, end):
            rows = super().get_prices("ARM", start, end)
            if ticker != "SMH":
                return rows
            # The sector fell half as much on the crash day and drifted the same way otherwise.
            out, close = [], 100.0
            for previous, bar in zip(rows, rows[1:]):
                step = float(bar.close) / float(previous.close)
                close *= 0.90 if step < 0.85 else step
                out.append(SimpleNamespace(time=bar.time, close=round(close, 2), volume=1))
            return out

    registry = CapabilityRegistry(default_catalog())
    now = datetime(2026, 7, 3, 18, 0, tzinfo=ZoneInfo("America/New_York"))
    register_market_capabilities(registry, price_source_factory=SectorPrices, move_provider=lambda ticker: None, now_factory=lambda: now, sector_for=lambda ticker: "SMH")
    result = registry.execute(PlanTask("d", "market.drawdown", {"ticker": "ARM", "loss_pct": -30.0, "top": 2}), _context())
    assert result.ok and result.metrics["window"] == "3m"
    span = next(item for item in result.evidence if item.metadata["evidence_scope"] == "benchmark_span")
    assert span.claim.startswith("同期行业基准 SMH 从 2026-05-19 到 ") and "ARM 比基准多跌 " in span.claim and result.metrics["benchmark_span"]["gap_pp"] > 5
    assert span.claim.rstrip("。") + f"[{span.id}]。" in result.metadata["narrative"]
    # An answer that skips the sector comparison is sent back; the narrative itself passes.
    skipped = verify_answer(f"ARM 从高点回撤 -37.40%[{result.evidence[1].id}]。", result.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result])
    assert any(w.startswith(f"回撤回答必须引用同期行业基准对比那条证据 [{span.id}]") for w in skipped.warnings), skipped
    assert verify_answer(result.metadata["narrative"], result.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok
    # No sector known: the block is simply absent, nothing fails.
    register_market_capabilities(registry, price_source_factory=Prices, move_provider=lambda ticker: None, now_factory=lambda: now, sector_for=lambda ticker: "")
    assert "benchmark_span" not in registry.execute(PlanTask("d", "market.drawdown", {"ticker": "ARM"}), _context()).metrics
    register_market_capabilities(registry, price_source_factory=Prices, move_provider=lambda ticker: None, now_factory=lambda: now)
    assert [row["date"] for row in result.metrics["worst_days"]] == ["2026-05-20", "2026-06-03"]
    assert result.metrics["peak"]["date"] == "2026-05-19" and result.metrics["drawdown"] < -0.15
    assert result.metadata["queries"] == ["why did ARM stock fall on 2026-05-20", "why did ARM stock fall on 2026-06-03"]
    narrative = result.metadata["narrative"]
    assert "2026-05-20 -20.00%" in narrative and "从 2026-05-19 的高点" in narrative
    assert verify_answer(narrative, result.evidence, answer_mode=AnswerMode.TOOL_GROUNDED, results=[result]).ok
    explicit = registry.execute(PlanTask("d", "market.drawdown", {"ticker": "ARM", "window": "1m"}), _context())
    assert explicit.metrics["window"] == "1m" and explicit.metrics["worst_days"]
    # A session still in progress is not a completed bar: the last row is dropped.
    intraday_now = datetime(2026, 7, 2, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    register_market_capabilities(registry, price_source_factory=Prices, move_provider=lambda ticker: None, now_factory=lambda: intraday_now)
    assert registry.execute(PlanTask("d", "market.drawdown", {"ticker": "ARM"}), _context()).metrics["as_of"] == "2026-07-01"


def test_history_capabilities_wrap_edgar_filings_and_the_anomaly_memory():
    from v2.agent_v2.adapters.history import register_history_capabilities

    registry = CapabilityRegistry(default_catalog())
    rows = [
        SimpleNamespace(filing_date="2026-08-05", form="8-K", accession_number="0001-25-000001", cik="0001973239"),
        SimpleNamespace(filing_date="2026-08-20", form="8-K", accession_number="0001-25-000002", cik="0001973239"),
    ]
    calls: list[tuple] = []
    foreign = [SimpleNamespace(filing_date="2026-07-30", form="6-K", accession_number="0001-25-000009", cik="0001973239")]

    def fetch(ticker, form, since, until):
        calls.append((ticker, form, since, until))
        if ticker == "ARM":
            return list(rows) if form == "8-K" else []
        return list(foreign) if form == "6-K" and ticker == "TSM" else []

    recalls = SimpleNamespace(date="2026-08-05", flags="gap_down,volume_spike", doc="ARM  gapped down after earnings;   guidance missed.")
    register_history_capabilities(registry, filings_fetch=fetch, anomaly_recall=lambda ticker, query, days: [recalls], today_factory=lambda: date(2026, 9, 9))
    filings = registry.execute(PlanTask("f", "filings.recent", {"ticker": "ARM", "forms": ["8-K", "10-Q"]}), _context())
    assert filings.ok and calls[0] == ("ARM", "8-K", "2025-09-09", "2026-09-09")
    assert [item.metadata["date"] for item in filings.evidence] == ["2026-08-20", "2026-08-05"]
    assert filings.evidence[0].source_url == "https://www.sec.gov/Archives/edgar/data/1973239/000125000002/"
    assert "2026-08-20 8-K[" in filings.metadata["narrative"]
    empty = registry.execute(PlanTask("f", "filings.recent", {"ticker": "ARM", "forms": ["10-Q"]}), _context())
    assert empty.ok and empty.evidence[0].metadata["citation_kind"] == "limitations" and "未查到 10-Q 申报" in empty.evidence[0].claim
    # A foreign private issuer has no 8-K; with no explicit form the adapter looks at 6-K before saying none.
    calls.clear()
    tsm = registry.execute(PlanTask("f", "filings.recent", {"ticker": "TSM"}), _context())
    assert [call[1] for call in calls] == ["8-K", "6-K"]
    assert tsm.ok and tsm.evidence[0].metadata["form"] == "6-K" and "2026-07-30 6-K[" in tsm.metadata["narrative"]
    calls.clear()
    none = registry.execute(PlanTask("f", "filings.recent", {"ticker": "XYZ"}), _context())
    assert [call[1] for call in calls] == ["8-K", "6-K"] and "未查到 8-K、6-K 申报" in none.evidence[0].claim
    anomalies = registry.execute(PlanTask("a", "market.anomaly_history", {"ticker": "ARM", "lookback_days": 365}), _context())
    assert anomalies.ok and anomalies.evidence[0].claim == "ARM 2026-08-05 盯盘记录：gap_down,volume_spike；ARM gapped down after earnings; guidance missed."
    register_history_capabilities(registry, filings_fetch=fetch, anomaly_recall=lambda *args: (_ for _ in ()).throw(RuntimeError("chroma down")))
    broken = registry.execute(PlanTask("a", "market.anomaly_history", {"ticker": "ARM"}), _context())
    assert not broken.ok and "anomaly memory unavailable" in broken.errors[0]


@pytest.mark.parametrize(
    ("query", "window", "is_drawdown"),
    [
        ("ARM 从 6 月高点为什么跌了这么多?", "", True),
        ("ARM 我买入以来为什么亏了 30%", "", True),
        ("QCOM 这几个月为什么一路跌", "", True),
        ("ARM 今年为什么回撤这么多", "1y", True),
        ("ARM 这个月为什么跌这么狠", "1m", True),
        ("NVDA 今天为什么跌这么多", "", False),
        ("NVDA 为什么涨了这么多", "", False),
        ("TSLA 为什么跌，内部人在卖吗，财报什么时候", "", False),
    ],
)
def test_rule_planner_opens_a_drawdown_from_the_wording_alone(query, window, is_drawdown):
    plan = _plan(query)
    capabilities = [task.capability for task in plan.tasks]
    if is_drawdown:
        assert capabilities == ["account.portfolio", "market.performance", "market.drawdown", "filings.recent", "market.anomaly_history", "market.attribute_move"]
        drawdown = plan.tasks[2].arguments
        assert "loss_pct" not in drawdown and drawdown.get("window", "") == window
        assert plan.assumptions[0].startswith("context_frame: 用户问的是") and "从高点以来或这段时间的跌幅" in plan.assumptions[0]
        llm = ScriptedLLM([LLMResponse(text="{}")])
        request = normalize_request(query)
        assert len(StructuredLLMPlanner(llm, default_catalog()).plan(request, route(request)).tasks) == 6 and llm.calls == []
    else:
        assert "market.drawdown" not in capabilities and "market.attribute_move" not in capabilities


def test_decline_timing_reads_the_return_windows():
    from v2.agent_v2.synthesis import catalyst_lines, decline_timing

    item = EvidenceItem("W", "ARM", "区间回报", metadata={"evidence_scope": "returns"})
    result = ToolEnvelope("market.performance", ResultStatus.COMPLETED, subject="ARM", evidence=[item])
    assert decline_timing(-32.0, {"5d": 0.12, "1m": -0.015, "3m": -0.214, "1y": -0.351}, result)[0].endswith("这段跌幅大部分落在近 3 月内[W]。")
    assert decline_timing(-32.0, {"5d": -0.20, "1m": -0.25}, result)[0].endswith("这段跌幅大部分落在近 5 日内[W]。")
    sentences = decline_timing(-32.0, {"5d": 0.01, "1m": -0.02, "3m": -0.05, "1y": 0.90}, result)
    assert sentences[0].endswith("这段跌幅主要发生在近 1 年以前[W]。") and sentences[1].startswith("近 1 年 +90.00% 而该持仓仍在浮亏")
    assert decline_timing(5.0, {"1m": -0.02}, result) == [] and decline_timing(-32.0, {}, result) == []
    undated = ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject="ARM", evidence=[EvidenceItem("F", "ARM", "TTM P/E is 377.2x.")], limitations=["expectations: 34/100"])
    assert catalyst_lines(undated) == "ARM 期间未查到可核对的催化剂（财报、公告或新闻）。\n数据限制：expectations: 34/100"


def test_sub_agent_loop_is_bounded_by_the_coordinators_remaining_time():
    import time

    from v2.agent_v2.agents.base import BoundedLoop, LoopLimits, limits_for
    from v2.agent_v2.agents.filing_reader import FilingReader, FilingRef

    # limits_for: the coordinator's remaining clock, minus a margin, caps the loop.
    roomy = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO, deadline=time.monotonic() + 600)
    assert limits_for(roomy, max_rounds=6, max_seconds=90).seconds == 90
    tight = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO, deadline=time.monotonic() + 30)
    assert 24 <= limits_for(tight, max_rounds=6, max_seconds=90).seconds <= 25
    assert limits_for(None, max_rounds=6, max_seconds=90).seconds == 90

    class Echo(BoundedLoop):
        def handle(self, action, messages):
            messages.append({"role": "user", "content": "ok"})
            return True

    # No budget left: the loop does not start and no model call is made.
    llm = ScriptedLLM([LLMResponse(text='{"action":"finish","events":[]}')])
    outcome = Echo(llm, LoopLimits(max_rounds=3, max_seconds=60, outer_seconds=4)).run("sys", "task", finish_prompt="finish")
    assert outcome.stop_reason == "no_budget" and outcome.calls == 0 and llm.calls == []
    assert Echo(None, LoopLimits()).run("sys", "task", finish_prompt="finish").stop_reason == "no_model"
    # A normal finish records rounds, calls and the final action.
    done = Echo(ScriptedLLM([LLMResponse(text='{"action":"read"}'), LLMResponse(text='{"action":"finish","x":1}')]), LoopLimits(max_rounds=3)).run("sys", "task", finish_prompt="finish")
    assert done.finished and done.final == {"action": "finish", "x": 1} and (done.rounds, done.calls, done.stop_reason) == (2, 2, "finished")

    # The engine hands the deadline to handlers: a reader called with almost no time left says so instead of reading.
    refs = [FilingRef("ARM", "8-K", "2026-07-29", "0001-26-000777", "https://www.sec.gov/x/777/")]
    reader = FilingReader(ScriptedLLM([LLMResponse(text='{"action":"finish","events":[]}')]), _FakeFilingSource(refs))
    starved = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO, deadline=time.monotonic() + 3)
    result = reader.run("ARM", starved, around="2026-07-29", today=date(2026, 9, 9))
    assert result.metrics["stop_reason"] == "no_budget" and result.metrics["llm_calls"] == 0 and "协调者剩余时间不足" in result.limitations[0]
    registry = CapabilityRegistry(default_catalog())
    seen: dict[str, float] = {}

    def probe(arguments, context):
        seen["remaining"] = context.remaining_seconds()
        return ToolEnvelope("account.portfolio", ResultStatus.COMPLETED, subject="portfolio", summary="x", evidence=[EvidenceItem("P", "portfolio", "x")])

    registry.register("account.portfolio", probe)
    context = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.DIRECT)  # DIRECT allows 30 s
    ExecutionEngine(registry).run(ExecutionPlan("q", RouteKind.FAST_LOOKUP, tasks=(PlanTask("p", "account.portfolio"),), budget=BudgetClass.DIRECT), context)
    assert 0 < seen["remaining"] <= 30 and context.deadline is not None


class _Bar:
    def __init__(self, time, close, volume=1_000_000):
        self.time, self.close, self.volume = time, close, volume


def _attributor_prices(ticker, start, end):
    first = date(2026, 1, 5)
    rows = []
    close = 300.0 if ticker == "ARM" else 100.0
    for index in range(260):
        day = first + timedelta(days=index)
        if day.weekday() >= 5 or day.isoformat() > str(end):
            continue
        if day == date(2026, 7, 29):
            close *= 0.92 if ticker == "ARM" else 0.99
        else:
            close *= 1.001
        rows.append(_Bar(day.isoformat(), round(close, 2), 3_000_000 if day == date(2026, 7, 29) and ticker == "ARM" else 1_000_000))
    return rows


def test_move_attributor_explains_a_past_day_from_sources_it_fetched_and_remembers_it():
    from v2.agent_v2.agents.move_attributor import MoveAttributor, day_facts

    prices = _attributor_prices("ARM", "2025-07-01", "2026-09-09")
    facts = day_facts("ARM", "2026-07-29", prices, "SMH", _attributor_prices("SMH", "2025-07-01", "2026-09-09"))
    assert facts.date == "2026-07-29" and abs(facts.change + 0.08) < 0.001
    assert facts.volume_ratio == 3.0 and facts.sector_return_1d is not None and facts.relative_1d < 0

    news_calls: list[str] = []

    def news(query, day):
        news_calls.append(query)
        return [
            {"title": "Arm falls as guidance disappoints", "url": "https://example.com/arm-guidance", "content": "Arm Holdings shares slid 8% on Wednesday after the company's revenue guidance came in below Wall Street expectations.", "published_date": "2026-07-29"},
            {"title": "Unrelated chip story", "url": "https://example.com/other", "content": "Nvidia rallied on strong demand.", "published_date": "2026-07-29"},
        ]

    class Reader:
        def run(self, ticker, context, *, around, today):
            return ToolEnvelope("filings.read_events", ResultStatus.COMPLETED, subject=ticker, evidence=[EvidenceItem("E-ARM-0729", "ARM", "ARM 2026-07-29：季度营收低于指引区间（6-K 2026-07-29 s1：“Revenue was below the guidance range”）。", as_of="2026-07-29", source_url="https://www.sec.gov/x/114/", metadata={"evidence_scope": "filing_event", "date": "2026-07-29", "quote": "Revenue was below the guidance range"})])

    remembered: list[tuple] = []
    memory = [SimpleNamespace(date="2026-07-29", flags="gap_down", doc="ARM gap_down 财报后跳空低开")]
    llm = ScriptedLLM(
        [
            LLMResponse(text='{"action":"news","query":"Arm Holdings stock July 29 2026 falls"}'),
            LLMResponse(text='{"action":"filing_events"}'),
            LLMResponse(text='{"action":"memory","query":"ARM 下跌"}'),
            LLMResponse(
                text=json.dumps(
                    {
                        "action": "finish",
                        "reasons": [
                            {"text": "营收指引低于华尔街预期", "confidence": "高", "source": {"kind": "news", "url": "https://example.com/arm-guidance"}, "quote": "revenue guidance came in below Wall Street expectations"},
                            {"text": "申报显示营收低于指引区间", "confidence": "中", "source": {"kind": "filing", "id": "E-ARM-0729"}, "quote": "Revenue was below the guidance range"},
                            {"text": "盯盘记录显示财报后跳空低开", "confidence": "高", "source": {"kind": "memory", "date": "2026-07-29"}, "quote": "财报后跳空低开"},
                            {"text": "编造：被收购传闻", "confidence": "高", "source": {"kind": "news", "url": "https://example.com/nowhere"}, "quote": "takeover rumours"},
                        ],
                        "next_steps": ["关注下季指引"],
                        "note": "新闻与申报一致",
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )
    attributor = MoveAttributor(llm, price_source_factory=lambda: SimpleNamespace(get_prices=_attributor_prices), news=news, filing_reader=Reader(), memory_recall=lambda ticker, query, days: memory, memory_remember=lambda facts, reasons: remembered.append((facts.date, [(r["text"], r["confidence"]) for r in reasons])) or "ARM_2026-07-29_retro", sector_for=lambda ticker: "SMH")
    context = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO, allow_web=True)
    result = attributor.run("ARM", context, day="2026-07-29", today=date(2026, 9, 9))
    assert result.ok and result.status == ResultStatus.COMPLETED and news_calls == ["Arm Holdings stock July 29 2026 falls"]
    drivers = [item for item in result.evidence if item.metadata.get("claim_role") == "confirmed_driver"]
    candidates = [item for item in result.evidence if item.metadata.get("claim_role") == "candidate_driver"]
    assert [item.metadata["driver_text"] for item in drivers] == ["营收指引低于华尔街预期"] and drivers[0].source_url == "https://example.com/arm-guidance"
    assert [(item.metadata["driver_text"], item.metadata["causal_confidence"]) for item in candidates] == [("申报显示营收低于指引区间", "中"), ("盯盘记录显示财报后跳空低开", "中")]  # memory-only support is capped at 中
    assert any(item.id == "E-ARM-0729" for item in result.evidence)  # the reader's event travels with the attribution
    assert "1 条原因没有可核对的来源，已丢弃" in result.limitations[0]
    assert result.metrics["confirmed_driver_count"] == 1 and result.metrics["news_calls"] == 1 and result.metrics["reader_calls"] == 1 and result.metrics["memory_calls"] == 1
    assert result.metrics["remembered_as"] == "ARM_2026-07-29_retro" and remembered == [("2026-07-29", [("营收指引低于华尔街预期", "高"), ("申报显示营收低于指引区间", "中"), ("盯盘记录显示财报后跳空低开", "中")])]
    narrative = result.metadata["narrative"]
    assert narrative.startswith("ARM 在 2026-07-29 收于") and "能直接支持的高置信度驱动：营收指引低于华尔街预期[" in narrative and "跑输行业基准 SMH" in narrative
    assert verify_answer(narrative, result.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok
    compact = result.metadata["narrative_compact"]
    assert compact.startswith("2026-07-29 ARM ") and "驱动：营收指引低于华尔街预期[" in compact and "跑输 SMH" in compact
    assert "\n" not in compact and len(compact) < len(narrative)
    assert verify_answer(compact, result.evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result]).ok
    # Without web consent the news action is refused and the loop is told so.
    refused = ScriptedLLM([LLMResponse(text='{"action":"news","query":"x"}'), LLMResponse(text='{"action":"finish","reasons":[],"note":"无新闻"}')])
    quiet = MoveAttributor(refused, price_source_factory=lambda: SimpleNamespace(get_prices=_attributor_prices), news=news, filing_reader=Reader(), memory_recall=None, memory_remember=None, sector_for=None)
    result = quiet.run("ARM", ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO), day="2026-07-29", today=date(2026, 9, 9))
    assert result.status == ResultStatus.PARTIAL_DATA and result.metrics["news_calls"] == 0 and len(news_calls) == 1
    assert "用户未授权网页搜索" in refused.calls[1][-1]["content"] and "归因未使用新闻" in " ".join(result.limitations)


def test_locate_quote_tolerates_punctuation_and_rejects_invention():
    from v2.agent_v2.agents.filing_reader import locate_quote

    text = "Revenue of $1,050 million was below the guidance range; the company now expects fiscal-year revenue growth in the “low twenties”. Nothing else."
    assert locate_quote("Revenue of $1,050 million was below the guidance range", text) == "Revenue of $1,050 million was below the guidance range"
    assert locate_quote('the company now expects fiscal-year revenue growth in the “low twenties”', text) == 'the company now expects fiscal-year revenue growth in the "low twenties"'
    assert locate_quote("the company now expects fiscal year revenue growth in the low-twenties", text) == 'the company now expects fiscal-year revenue growth in the "low twenties".'
    assert locate_quote("the company was acquired by a competitor last week", text) is None
    assert locate_quote("", text) is None and locate_quote("anything", "") is None


def test_framed_answer_renders_web_results_by_headline_and_date_and_coverage_as_one_line():
    from v2.agent_v2.synthesis import web_lines

    result = ToolEnvelope(
        "web.research",
        ResultStatus.COMPLETED,
        subject="ARM",
        evidence=[
            EvidenceItem("W1", "ARM", "Arm shares slid 8% after guidance came in below expectations.", as_of="2026-07-30T12:00:00", source_title="Arm falls on soft outlook", source_url="https://example.com/a", metadata={"evidence_type": "search_snippet"}),
            EvidenceItem("W2", "ARM", "An older story.", as_of="2026-03-01", source_title="Arm rallies", source_url="https://example.com/b", metadata={"evidence_type": "search_snippet"}),
            EvidenceItem("W3", "ARM", "Undated aggregator page " * 20, source_title="Stock page", source_url="https://example.com/c", metadata={"evidence_type": "search_snippet"}),
        ],
        limitations=["Evidence contains search-result snippets; source pages were not fetched in this adapter."],
    )
    lines = web_lines(result, "2026-06-10").split("\n")
    assert lines[0] == "- Arm falls on soft outlook（2026-07-30）：Arm shares slid 8% after guidance came in below expectations. [W1]"
    assert "[W2]" not in "\n".join(lines) and lines[1].startswith("- Stock page（日期未知）：") and lines[1].endswith("… [W3]")
    assert web_lines(ToolEnvelope("web.research", ResultStatus.COMPLETED, subject="ARM"), "2026-06-10") == "ARM 网页搜索未返回落在区间内的报道。"


def test_frame_lead_drops_a_sentence_its_own_verifier_rejects():
    from v2.agent_v2.adapters.legacy import _wrap
    from v2.agent_v2.synthesis import frame_lead

    portfolio = _wrap("account.portfolio", "portfolio", _PORTFOLIO_CARD)
    # A price item whose rule no generated wording can satisfy: the aside must be dropped, the rest kept.
    price = EvidenceItem("P", "ARM", "ARM 盘中 +0.94%。", metadata={"evidence_scope": "price", "constraints": [{"require": "永远不会出现的标记", "warning": "rule"}]})
    windows = EvidenceItem("W", "ARM", "ARM 区间回报：1d +0.94%，1m -1.53%，3m -21.40%。", metadata={"evidence_scope": "returns"})
    performance = ToolEnvelope("market.performance", ResultStatus.COMPLETED, subject="ARM", metrics={"returns": {"1d": 0.0094, "1m": -0.0153, "3m": -0.2140}}, evidence=[price, windows])
    frame = {"kind": "position", "ticker": "ARM", "field": "pl_pct", "text": "pl_pct_text", "label": "买入以来的浮动盈亏"}
    lead = frame_lead(frame, [portfolio, performance])
    assert "今日" not in lead and "这段跌幅大部分落在近 3 月内[W]" in lead


def test_agent_v2_seed_eval_passes_offline():
    report = run_suite()
    assert report.passed == report.total


def test_every_telegram_handler_keeps_its_owner_guard():
    """A helper inserted between @authorized_only and its handler once stole the decorator; never again."""

    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("bot", "commands.py").read_text(encoding="utf-8")
    handlers = re.findall(r"^(@authorized_only\n)?async def (cmd_\w+)\(", source, re.M)
    assert handlers, "no handlers found"
    assert [name for decorator, name in handlers if not decorator] == []
    assert re.search(r"^def _free_text_agent\(\) -> str:", source, re.M) and not re.search(r"@authorized_only\ndef _free_text_agent", source)


def _require_telegram() -> None:
    """Skip when python-telegram-bot is absent or its native deps fail to load (a sandbox, not a bug)."""

    import importlib

    try:
        importlib.import_module("telegram")
    except BaseException as exc:  # noqa: BLE001 — pyo3 raises a PanicException, not ImportError
        pytest.skip(f"telegram unavailable: {type(exc).__name__}")


def test_telegram_plain_messages_go_to_agent_v2_and_ask_keeps_v1(monkeypatch):
    _require_telegram()
    from v2.bot import agent_v2_bridge, commands

    called: list[dict] = []

    async def handle(update, context, text, *, allow_web=False):
        called.append({"text": text, "allow_web": allow_web})

    class Message:
        def __init__(self, text):
            self.text = text
            self.replies = []

        async def reply_html(self, text, **kwargs):
            self.replies.append(text)
            return self

        async def edit_text(self, text, **kwargs):
            self.replies.append(text)

    class Chat:
        id = 7

    def update_for(text):
        return type("Update", (), {"message": Message(text), "effective_chat": Chat()})()

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    monkeypatch.delenv("TELEGRAM_FREE_TEXT_AGENT", raising=False)
    monkeypatch.setattr(agent_v2_bridge, "handle_agent_v2", handle)
    monkeypatch.delenv("TELEGRAM_WEB_DEFAULT", raising=False)
    update = update_for("为什么跌这么狠")
    asyncio.run(commands.cmd_nl(update, object()))
    assert called == [{"text": "为什么跌这么狠", "allow_web": True}] and update.message.replies == []
    asyncio.run(commands.cmd_nl(update_for("为什么跌这么狠 --noweb"), object()))
    assert called[-1] == {"text": "为什么跌这么狠", "allow_web": False}
    handled = len(called)
    # A rollback switch hands plain messages back to the V1 chain.
    monkeypatch.setenv("TELEGRAM_FREE_TEXT_AGENT", "v1")
    monkeypatch.setattr(commands, "agent_bridge", None)
    monkeypatch.setattr(commands.intent, "classify", lambda text: {"intent": "unknown", "ticker": "", "manager": ""})

    async def unknown(*args, **kwargs):
        raise RuntimeError("stop here")

    monkeypatch.setattr(commands, "_run_blocking", lambda func, *args: asyncio.sleep(0, result=func(*args)))
    rolled_back = update_for("为什么跌这么狠")
    try:
        asyncio.run(commands.cmd_nl(rolled_back, object()))
    except Exception:  # noqa: BLE001 — the V1 chain needs more scaffolding than this test provides
        pass
    assert len(called) == handled and rolled_back.message.replies[:1] == ["🤔 理解中..."]


def test_telegram_ask_v2_command_is_explicit_and_parses_web_consent(monkeypatch):
    _require_telegram()
    from v2.bot import agent_v2_bridge, commands

    called = {}

    async def handle(update, context, text, *, allow_web=False):
        called.update({"text": text, "allow_web": allow_web})

    class Message:
        async def reply_html(self, text, **kwargs):
            pytest.fail(f"unexpected usage response: {text}")

    class Chat:
        id = 7

    class Update:
        message = Message()
        effective_chat = Chat()

    class Context:
        args = ["--noweb", "比较", "NVDA", "和", "AMD"]

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    monkeypatch.setattr(agent_v2_bridge, "handle_agent_v2", handle)
    asyncio.run(commands.cmd_agent_v2(Update(), Context()))
    assert called == {"text": "比较 NVDA 和 AMD", "allow_web": False}


def test_telegram_web_consent_defaults_on_with_an_opt_out(monkeypatch):
    from v2.bot.agent_v2_bridge import split_web_consent

    monkeypatch.delenv("TELEGRAM_WEB_DEFAULT", raising=False)
    assert split_web_consent("为什么跌这么狠") == ("为什么跌这么狠", True)
    assert split_web_consent("为什么跌这么狠 --noweb") == ("为什么跌这么狠", False)
    assert split_web_consent("--WEB 为什么跌这么狠 --noweb") == ("为什么跌这么狠", False)
    monkeypatch.setenv("TELEGRAM_WEB_DEFAULT", "0")
    assert split_web_consent("为什么跌这么狠") == ("为什么跌这么狠", False)
    assert split_web_consent("--web 为什么跌这么狠") == ("为什么跌这么狠", True)


def _telegram_result(answer: str, *, outcome: str = "fallback", warnings: tuple[str, ...] = ()):
    from v2.agent_v2.models import AgentResult, AnswerMode, ExecutionPlan, NormalizedRequest, ResultStatus, RouteDecision, RouteKind, RunStatus, ToolEnvelope, VerificationReport

    price = EvidenceItem("evidence-market-price-42f5c41951e36ef1", "ARM", "ARM 在 2026-07-29 收于 149.35，当日 -13.21%。", source_id="market_data")
    news = EvidenceItem("evidence-news-1", "ARM", "Arm Holdings slides after guidance disappoints; the stock fell 13%.", source_id="web_news", source_title="Arm slides on soft guidance", source_url="https://example.com/arm")
    full = "ARM 在 2026-07-29 收于 149.35，当日 -13.21%[evidence-market-price-42f5c41951e36ef1]。当日成交量为 30 日均量的 4 倍。\n\n能直接支持的高置信度驱动：营收指引低于预期[evidence-news-1]。\n\n从盘面看，当天跑输行业基准 SMH 约 10.00%。"
    compact = "2026-07-29 ARM -13.21%[evidence-market-price-42f5c41951e36ef1]，跑输 SMH 约 10.00%。驱动：营收指引低于预期[evidence-news-1]。"
    envelope = ToolEnvelope("market.attribute_move", ResultStatus.COMPLETED, subject="ARM", evidence=[price, news], metadata={"narrative": full, "narrative_compact": compact})
    request = NormalizedRequest("为什么跌这么狠", "为什么跌这么狠")
    return AgentResult(
        "run", request, RouteDecision(RouteKind.RESEARCH, ("research",), "why"), ExecutionPlan(objective="q", route=RouteKind.RESEARCH),
        RunStatus.COMPLETED, answer.replace("{full}", full), AnswerMode.RESEARCH_GROUNDED, results=[envelope], evidence=[price, news],
        verification=VerificationReport(ok=not warnings, warnings=warnings),
        synthesis={"outcome": outcome, "attempts": [{"stage": "draft", "ok": False, "warnings": ["行情事实缺少邻近引用"]}, {"stage": "repair", "ok": False, "unknown_citations": ["results.metrics"]}] if outcome == "fallback" else []},
    )


def test_telegram_delivery_numbers_citations_and_compacts_worst_days(monkeypatch):
    from v2.agent_v2.interfaces import telegram_format
    from v2.bot.agent_v2_bridge import TelegramBotTransport

    result = _telegram_result("ARM 自买入以来浮亏 20%[evidence-market-price-42f5c41951e36ef1]。\n\n{full}", warnings=("未确认直接驱动时展示了过多弱候选线索",))
    numbered = telegram_format.number_citations(telegram_format.compact_attributions(result.answer, result), result.evidence)
    assert numbered.ids == ("evidence-market-price-42f5c41951e36ef1", "evidence-news-1")
    assert numbered.text == "ARM 自买入以来浮亏 20%[1]。\n\n2026-07-29 ARM -13.21%[1]，跑输 SMH 约 10.00%。驱动：营收指引低于预期[2]。"
    # Brackets that are not evidence ids are left alone.
    assert telegram_format.number_citations("ARM [2026-07-29] 跌 [evidence-news-1]", result.evidence).text == "ARM [2026-07-29] 跌 [1]"
    entries = telegram_format.source_entries(numbered.ids, result.evidence)
    assert [(entry.numbers, entry.label, entry.url) for entry in entries] == [
        ("1", "日线行情", ""),
        ("2", "Arm slides on soft guidance · Arm Holdings slides after guidance disappoints; the stock f…", "https://example.com/arm"),
    ]
    # Unlinked items are one line per origin, with their numbers as ranges.
    many = [EvidenceItem(f"m{i}", "ARM", f"row {i}", source_id="market_data") for i in range(1, 8)]
    many[3] = EvidenceItem("m4", "ARM", "card\n━━━\nrow", source_title="Existing deterministic responder")
    grouped = telegram_format.source_entries(tuple(item.id for item in many), many)
    assert [(entry.numbers, entry.label) for entry in grouped] == [("1–3、5–7", "日线行情"), ("4", "账户卡片")]
    filing = EvidenceItem("f1", "ARM", "ARM 于 2026-07-29 向 SEC 提交了 6-K（0001）。", source_id="sec_edgar", source_title="ARM 6-K 2026-07-29", source_url="https://www.sec.gov/x")
    assert telegram_format.source_entries(("f1",), [filing])[0].label == "ARM 6-K 2026-07-29"

    class Placeholder:
        sent: list[str] = []

        async def edit_text(self, text, **kwargs):
            self.sent.append(text)

    monkeypatch.setenv("AGENT_V2_WEB_ENABLED", "1")
    placeholder = Placeholder()
    transport = TelegramBotTransport(object(), placeholder, web_requested=False)
    asyncio.run(transport.deliver(7, result))
    (message,) = placeholder.sent
    header, _, body = message.partition("\n\n")
    assert "合成：兜底摘要" in header and "校验：有警告（1）" in header and "网页：已关闭（去掉 --noweb 可用新闻归因）" in header
    assert "<i>⚠ 校验：未确认直接驱动时展示了过多弱候选线索</i>" in header and "<i>兜底原因：初稿：行情事实缺少邻近引用；修正稿：未知引用 results.metrics</i>" in header
    assert "[evidence-" not in body and "[1]。" in body and "跑输 SMH" in body and "当日成交量" not in body
    assert body.endswith('<b>来源</b>\n1. 日线行情\n2. <a href="https://example.com/arm">Arm slides on soft guidance · Arm Holdings slides after guidance disappoints; the stock f…</a>')
    # A model-written answer never contains the narrative verbatim and is delivered as written.
    clean = _telegram_result("模型自己的话[evidence-news-1]。", outcome="clean")
    transport = TelegramBotTransport(object(), placeholder, web_requested=True)
    asyncio.run(transport.deliver(7, clean))
    assert "合成：模型回答 · 校验：通过 · 网页：已启用" in placeholder.sent[-1] and "模型自己的话[1]。" in placeholder.sent[-1]
    clean.synthesis["citation_completions"] = ["439.46 → [x]", "12.52 → [y]"]
    asyncio.run(transport.deliver(7, clean))
    assert "合成：模型回答，引用补全 2 处 · 校验：通过" in placeholder.sent[-1]
    repaired = _telegram_result("模型自己的话[evidence-news-1]。", outcome="repaired")
    repaired.synthesis["attempts"] = [{"stage": "draft", "ok": False, "warnings": ["回撤回答必须引用同期行业基准对比那条证据 [D-span]（…）"]}, {"stage": "repair", "ok": True}]
    asyncio.run(transport.deliver(7, repaired))
    assert "合成：模型回答（修正一轮）" in placeholder.sent[-1] and "<i>修正原因：初稿：回撤回答必须引用同期行业基准对比那条证据 [D-span]（…）</i>" in placeholder.sent[-1]
    assert "兜底原因" not in placeholder.sent[-1]
    assert "⚠ 校验" not in placeholder.sent[-1] and "兜底原因" not in placeholder.sent[-1]
    monkeypatch.setenv("AGENT_V2_WEB_ENABLED", "0")
    asyncio.run(transport.deliver(7, clean))
    assert "网页：未启用（服务端 AGENT_V2_WEB_ENABLED 未开）" in placeholder.sent[-1]


def test_workspace_lab_port_reuses_an_injected_runner_and_builds_evidence():
    progress = []

    class Input:
        def __init__(self, **values):
            self.values = values

    def runner(body, on_tick=None):
        assert body.values["strategy"] == "momentum"
        on_tick(3)
        return {
            "kind": "backtest",
            "strategy": "momentum",
            "tickers": ["NVDA"],
            "metrics": {"total_return_pct": 0.12, "n_trades": 8},
            "trades": [{"ticker": "NVDA", "return_pct": 0.03}],
        }

    context = ExecutionContext(
        "lab-run",
        NormalizedRequest("回测", "回测"),
        BudgetClass.LAB,
        on_progress=lambda event: progress.append(event.message),
    )
    port = WorkspaceLabPort({"lab.backtest": LabBinding(Input, runner, supports_progress=True)})
    result = port.run("lab.backtest", {"strategy": "momentum"}, context)
    assert result.ok
    assert result.metrics["total_return_pct"] == 0.12
    assert any(item.metric == "n_trades" for item in result.evidence)
    assert progress == ["lab.backtest: completed 3 work unit(s)"]


def test_tavily_web_adapter_bounds_and_deduplicates_search_evidence():
    class Provider:
        last_diagnostics = {"provider": "fake"}

        def search(self, query, *, days, max_results):
            assert query == "NVDA latest product"
            assert days == 30 and max_results == 2
            return [
                {
                    "title": "Source A",
                    "content": "A" * 600,
                    "url": "https://example.test/a#section",
                    "score": 0.8,
                },
                {
                    "title": "Duplicate",
                    "content": "duplicate",
                    "url": "https://example.test/a",
                },
                {"title": "Unsafe", "content": "ignored", "url": "file:///tmp/a"},
            ]

    result = TavilyWebSearchPort(Provider(), max_results=2, max_content_chars=300).search("NVDA latest product", topic="company_event", ticker="NVDA")
    assert result.ok
    assert len(result.evidence) == 1
    assert result.evidence[0].source_url == "https://example.test/a"
    assert len(result.evidence[0].claim) == 300
    assert result.evidence[0].metadata["evidence_type"] == "search_snippet"


def test_web_fallback_requires_runtime_and_per_request_opt_in():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    calls = []

    def failed_research(args, context):
        return ToolEnvelope("research.stock", ResultStatus.FAILED, errors=["provider down"])

    def web(args, context):
        calls.append(args["query"])
        item = EvidenceItem("WEB1", "NVDA", "A current source supports the event.")
        return ToolEnvelope(
            "web.research",
            ResultStatus.COMPLETED,
            summary=item.claim,
            evidence=[item],
        )

    registry.register("research.stock", failed_research)
    registry.register("web.research", web)
    agent = AgentV2(
        catalog=catalog,
        registry=registry,
        config=AgentV2Config(enable_web_fallback=True),
    )
    disabled = agent.run("分析 NVDA 的最新事件", allow_web=False)
    enabled = agent.run("分析 NVDA 的最新事件", allow_web=True)
    assert all(result.capability != "web.research" for result in disabled.results)
    assert calls == ["分析 NVDA 的最新事件"]
    assert enabled.answer_mode == AnswerMode.WEB_GROUNDED
    assert enabled.plan.tasks[-1].capability == "web.research"
    assert "[WEB1]" in enabled.answer


def test_router_requires_a_user_state_object_before_treating_english_verbs_as_commands():
    assert route(normalize_request("AVGO 的 total addressable market 有多大")).kind != RouteKind.COMMAND
    assert route(normalize_request("NVDA 加入标普指数会怎样")).kind != RouteKind.COMMAND
    assert route(normalize_request("add NVDA to my watchlist")).kind == RouteKind.COMMAND
    assert route(normalize_request("set an alert for AAPL")).kind == RouteKind.COMMAND
    assert route(normalize_request("删除 TSLA 提醒")).kind == RouteKind.COMMAND


@pytest.mark.parametrize(
    ("query", "entities"),
    [
        ("分析 nvda 的估值", ("NVDA",)),
        ("分析英伟达的估值", ("NVDA",)),
        ("比较阿里巴巴和拼多多", ("BABA", "PDD")),
        ("V 最近表现怎么样", ("V",)),
        ("BRK.B 估值高吗", ("BRK.B",)),
        ("what is the cost now", ()),
        ("t+1 结算规则", ()),
        ("NVDA 的 EPS 和 ROE", ("NVDA",)),
    ],
)
def test_entities_resolve_aliases_and_known_symbols(query, entities):
    assert normalize_request(query).entities == entities


def test_llm_planner_trims_an_over_budget_plan_instead_of_failing():
    rows = [
        {"id": "t1", "capability": "research.stock", "arguments": {"ticker": "NVDA", "focus": "valuation"}},
        {"id": "t2", "capability": "research.stock", "arguments": {"ticker": "NVDA", "focus": "earnings"}},
        {"id": "t3", "capability": "research.stock", "arguments": {"ticker": "NVDA", "focus": "risk"}},
        {"id": "t4", "capability": "market.performance", "arguments": {"ticker": "NVDA"}, "required": False},
        {"id": "t5", "capability": "market.explain_move", "arguments": {"ticker": "NVDA"}, "depends_on": ["t4"]},
        {"id": "t6", "capability": "research.changes", "arguments": {"ticker": "NVDA"}},
    ]
    llm = ScriptedLLM([LLMResponse(text=json.dumps({"objective": "x", "tasks": rows}))])
    catalog = default_catalog()
    request = normalize_request("深入分析 NVDA 的估值、财报和风险")
    plan = StructuredLLMPlanner(llm, catalog).plan(request, route(request))
    prompt = json.loads(llm.calls[0][1]["content"])
    assert prompt["maximum_tasks"] == 5
    assert plan.budget == BudgetClass.STANDARD
    assert [task.id for task in plan.tasks] == ["t1", "t2", "t3", "t6"]
    assert any("trimmed" in value for value in plan.assumptions)
    ExecutionEngine(CapabilityRegistry(catalog)).run(plan, ExecutionContext("run", request, plan.budget))


def _research_fixture():
    request = normalize_request("分析 NVDA 的增长")
    plan = ExecutionPlan("分析 NVDA 的增长", RouteKind.RESEARCH, answer_mode=AnswerMode.RESEARCH_GROUNDED)
    evidence = [EvidenceItem("E1", "NVDA", "NVDA revenue growth was 10%.")]
    results = [ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject="NVDA", summary="NVDA revenue growth was 10%.", evidence=evidence)]
    return request, plan, results, evidence


def test_llm_synthesizer_repairs_an_ungrounded_draft_once():
    llm = ScriptedLLM([LLMResponse(text="NVDA 收入增长 20%。[E1]"), LLMResponse(text="NVDA 收入增长 10%。[E1]")])
    request, plan, results, evidence = _research_fixture()
    answer = LLMEvidenceSynthesizer(llm).synthesize(request, plan, results, evidence)
    assert answer == "NVDA 收入增长 10%。[E1]"
    assert len(llm.calls) == 2
    repair = llm.calls[1]
    assert repair[2] == {"role": "assistant", "content": "NVDA 收入增长 20%。[E1]"}
    assert "20" in repair[3]["content"]
    assert "完整回答" in repair[3]["content"]


def test_llm_synthesizer_falls_back_to_deterministic_prose_when_repair_still_fails():
    llm = ScriptedLLM([LLMResponse(text="NVDA 收入增长 20%。[E1]"), LLMResponse(text="NVDA 收入增长 25%。[E1]")])
    request, plan, results, evidence = _research_fixture()
    answer = LLMEvidenceSynthesizer(llm).synthesize(request, plan, results, evidence)
    assert "20%" not in answer and "25%" not in answer
    assert "[E1]" in answer
    assert verify_answer(answer, evidence, answer_mode=plan.answer_mode, results=results).ok


def _mutation_agent(applied: list):
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def mutate(arguments, context):
        applied.append(arguments)
        return ToolEnvelope("state.mutate", ResultStatus.COMPLETED, subject=arguments["operation"], summary="已将 NVDA 加入关注列表。", evidence=[EvidenceItem("M1", "NVDA", "已将 NVDA 加入关注列表。")])

    registry.register("state.mutate", mutate)
    return AgentV2(catalog=catalog, registry=registry, session=ShortTermSession())


def test_command_is_parsed_held_for_confirmation_and_applied_only_after_confirm():
    applied: list = []
    agent = _mutation_agent(applied)
    first = agent.run("把 NVDA 加入关注列表", session_id="chat-1")
    assert first.status == RunStatus.WAITING_CONFIRMATION
    assert first.pending_mutation is not None
    assert first.pending_mutation.operation == "watchlist.add"
    assert first.pending_mutation.payload == {"ticker": "NVDA"}
    assert "确认" in first.answer
    assert not applied
    second = agent.run("确认", session_id="chat-1")
    assert second.status == RunStatus.COMPLETED
    assert applied == [{"operation": "watchlist.add", "payload": {"ticker": "NVDA"}}]
    assert second.results[0].capability == "state.mutate"
    assert second.verification.ok
    third = agent.run("确认", session_id="chat-1")
    assert third.status != RunStatus.COMPLETED or not third.results
    assert len(applied) == 1


def test_command_cancel_or_new_question_drops_the_pending_mutation():
    applied: list = []
    agent = _mutation_agent(applied)
    agent.run("NVDA 涨到 200 美元提醒我", session_id="chat-2")
    cancelled = agent.run("取消", session_id="chat-2")
    assert cancelled.status == RunStatus.CANCELLED
    assert agent.run("确认", session_id="chat-2").results == []
    agent.run("把 AMD 加入关注列表", session_id="chat-3")
    moved_on = agent.run("什么是自由现金流？", session_id="chat-3")
    assert moved_on.route.kind == RouteKind.GENERAL_KNOWLEDGE
    assert agent.run("确认", session_id="chat-3").results == []
    assert not applied


def test_command_without_a_session_or_with_missing_parameters_does_not_wait_forever():
    applied: list = []
    agent = _mutation_agent(applied)
    no_session = agent.run("把 NVDA 加入关注列表")
    assert no_session.status == RunStatus.WAITING_CONFIRMATION
    assert "无法接收确认" in no_session.answer
    incomplete = agent.run("取消 NVDA 的提醒", session_id="chat-4")
    assert incomplete.status == RunStatus.PARTIAL
    assert "提醒编号" in incomplete.answer
    assert incomplete.pending_mutation is None
    assert not applied


def test_state_mutate_adapter_maps_operations_onto_bot_state(monkeypatch):
    import sys
    from types import ModuleType

    from v2.agent_v2.adapters.legacy import register_legacy_capabilities

    calls: list = []
    fake = ModuleType("v2.bot.state")
    fake.watchlist_add = lambda ticker, note="": calls.append(("add", ticker)) or True
    fake.watchlist_remove = lambda ticker: calls.append(("remove", ticker)) or False
    fake.alert_add = lambda ticker, direction, target: calls.append(("alert", ticker, direction, target)) or 7
    fake.alert_remove = lambda alert_id: calls.append(("unalert", alert_id)) or True
    monkeypatch.setitem(sys.modules, "v2.bot.state", fake)
    registry = CapabilityRegistry(default_catalog())
    register_legacy_capabilities(registry)
    context = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.DIRECT, allow_mutations=True)
    added = registry.execute(PlanTask("m", "state.mutate", {"operation": "watchlist.add", "payload": {"ticker": "nvda"}}), context)
    assert added.ok and "加入关注列表" in added.summary and added.evidence
    removed = registry.execute(PlanTask("m", "state.mutate", {"operation": "watchlist.remove", "payload": {"ticker": "AMD"}}), context)
    assert "不在关注列表" in removed.summary
    alert = registry.execute(PlanTask("m", "state.mutate", {"operation": "alert.add", "payload": {"ticker": "AAPL", "direction": "below", "target_price": 150}}), context)
    assert "#7" in alert.summary and "跌到" in alert.summary
    unalert = registry.execute(PlanTask("m", "state.mutate", {"operation": "alert.remove", "payload": {"alert_id": 7}}), context)
    assert "已取消提醒 #7" in unalert.summary
    assert calls == [("add", "NVDA"), ("remove", "AMD"), ("alert", "AAPL", "below", 150.0), ("unalert", 7)]
    blocked = registry.execute(PlanTask("m", "state.mutate", {"operation": "watchlist.add", "payload": {"ticker": "NVDA"}}), _context())
    assert not blocked.ok and "confirmation" in blocked.errors[0]


def test_executor_enforces_the_wall_clock_budget():
    import time as _time

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def slow(arguments, context):
        _time.sleep(0.5)
        return ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject="NVDA", evidence=[EvidenceItem("S1", "NVDA", "slow")])

    def fast(arguments, context):
        return ToolEnvelope("account.portfolio", ResultStatus.COMPLETED, subject="portfolio", evidence=[EvidenceItem("F1", "portfolio", "fast")])

    registry.register("research.stock", slow)
    registry.register("account.portfolio", fast)
    plan = ExecutionPlan(
        "q",
        RouteKind.RESEARCH,
        tasks=(
            PlanTask("slow", "research.stock", {"ticker": "NVDA"}),
            PlanTask("fast", "account.portfolio"),
            PlanTask("after", "account.risk", depends_on=("slow",)),
        ),
        budget=BudgetClass.PORTFOLIO,
    )
    context = ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO, deadline=_time.monotonic() + 0.1)
    outcome = ExecutionEngine(registry).run(plan, context)
    by_capability = {result.capability: result for result in outcome.results}
    assert outcome.stop_reason == "deadline"
    assert by_capability["account.portfolio"].ok
    assert by_capability["research.stock"].status == ResultStatus.FAILED and "timed out" in by_capability["research.stock"].errors[0]
    assert by_capability["account.risk"].status == ResultStatus.SKIPPED
    assert outcome.ledger.ids() == {"F1"}


def test_orchestrator_surfaces_deadline_as_partial_with_a_stop_reason():
    import time as _time

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def slow(arguments, context):
        _time.sleep(0.3)
        return ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject="NVDA", evidence=[EvidenceItem("S1", "NVDA", "slow")])

    registry.register("research.stock", slow)
    agent = AgentV2(catalog=catalog, registry=registry, config=AgentV2Config(max_seconds=0.05))
    result = agent.run("分析 NVDA 的风险")
    assert result.status == RunStatus.PARTIAL
    assert result.stop_reason == "deadline"
    assert result.to_dict()["stop_reason"] == "deadline"
    assert "timed out" in result.results[0].errors[0]


def test_async_lab_requests_execute_inline_and_half_finished_lab_result_is_gone():
    assert default_catalog().get("lab.result") is None
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    registry.register("lab.sweep", lambda arguments, context: ToolEnvelope("lab.sweep", ResultStatus.COMPLETED, subject="sp500", summary="sweep done", evidence=[EvidenceItem("L1", "sp500", "sweep done")]))
    result = AgentV2(catalog=catalog, registry=registry).run("对标普全部股票做十年参数扫描")
    assert result.route.kind == RouteKind.ASYNC and result.route.asynchronous
    assert result.status == RunStatus.COMPLETED
    assert result.results[0].capability == "lab.sweep"


def _plan(query: str) -> ExecutionPlan:
    request = normalize_request(query)
    return RulePlanner().plan(request, route(request))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("最近 CPI", {"macro.release"}),
        ("宏观怎么样，还有最近 CPI", {"macro.overview", "macro.release"}),
        ("巴菲特最新持仓", {"institutional.manager_portfolio"}),
        ("巴菲特买了什么，ARKK 又买了什么", {"institutional.manager_portfolio", "etf.ark_activity"}),
        ("推送阈值是多少", {"state.read"}),
        ("我关注了哪些股票", {"state.read"}),
        ("未来两周谁要发财报", {"account.earnings_schedule"}),
        ("我这周亏的钱今天补回来了吗", {"account.performance"}),
        ("我的当日盈亏和组合风险", {"account.performance", "account.risk", "account.portfolio"}),
        ("TSLA 和 PLTR 哪个逆势更严重", {"market.explain_move"}),
        ("NVDA 涨了吗？资金流呢？", {"market.explain_move", "research.stock"}),
        ("NVDA 和 AMD 谁的财报更好", {"research.compare"}),
        ("AAPL 财报怎么样，另外内部人有没有在卖", {"research.stock"}),
        ("我的组合和 ARKK 有重叠吗", {"account.portfolio", "etf.ark_activity"}),
        ("现在是加仓的好时候吗", {"macro.overview", "account.risk"}),
        ("帮我看看要不要减仓", {"macro.overview", "account.risk", "account.portfolio"}),
        ("CRWD 占仓多少，超没超过集中度阈值", {"account.risk", "state.read", "research.stock"}),
        ("我的仓库里哪只跌的最多?", {"account.portfolio"}),
        ("我的仓库里今天哪只跌的最多?", {"account.portfolio", "market.explain_move"}),
        ("仓库里哪个亏最多", {"account.performance", "account.portfolio"}),
    ],
)
def test_rule_planner_covers_the_capabilities_the_v1_benchmark_needs(query, expected):
    plan = _plan(query)
    assert {task.capability for task in plan.tasks} == expected, [task.capability for task in plan.tasks]


def test_rule_planner_fans_per_ticker_topics_out_over_holdings_and_watchlist():
    plan = _plan("我持仓里有没有内部人在卖")
    assert plan.tasks[0].capability == "account.portfolio"
    template = next(task for task in plan.tasks if task.fan_out)
    assert template.capability == "research.stock" and template.arguments == {"focus": "ownership"}
    assert template.fan_out["from"] == "account-portfolio" and template.depends_on == ("account-portfolio",)
    assert plan.budget == BudgetClass.PORTFOLIO
    watch = _plan("关注列表里那几只最近怎么样")
    assert watch.tasks[0].capability == "state.read" and watch.tasks[0].arguments == {"section": "watchlist"}
    assert watch.tasks[1].capability == "market.explain_move" and watch.tasks[1].fan_out["from"] == "state-watchlist"
    assert _plan("TSLA 什么时候发财报").tasks[0].arguments == {"ticker": "TSLA", "focus": "earnings"}


def test_rule_planner_answers_help_directly_and_asks_for_missing_command_details():
    plan = _plan("你能帮我做什么")
    assert not plan.tasks and "持仓" in plan.direct_answer
    result = AgentV2().run("你能帮我做什么")
    assert result.status == RunStatus.COMPLETED and result.answer == plan.direct_answer
    clarification = AgentV2().run("取消 NVDA 的提醒")
    assert clarification.status == RunStatus.PARTIAL and "提醒编号" in clarification.answer


def test_executor_expands_fan_out_tasks_from_the_source_result():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    registry.register("account.portfolio", lambda a, c: ToolEnvelope("account.portfolio", ResultStatus.COMPLETED, subject="portfolio", evidence=[EvidenceItem("P", "portfolio", "holdings")], metadata={"tickers": ["NVDA", "AMD", "CRWD"]}))
    registry.register("research.stock", lambda a, c: ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject=a["ticker"], evidence=[EvidenceItem(f"R-{a['ticker']}", a["ticker"], f"{a['ticker']} {a['focus']}")]))
    registry.register("account.risk", lambda a, c: ToolEnvelope("account.risk", ResultStatus.COMPLETED, subject="portfolio", evidence=[EvidenceItem("K", "portfolio", "risk")]))
    plan = ExecutionPlan(
        "q",
        RouteKind.RESEARCH,
        tasks=(
            PlanTask("holdings", "account.portfolio"),
            PlanTask("each", "research.stock", {"focus": "filings"}, depends_on=("holdings",), fan_out={"from": "holdings", "field": "tickers", "argument": "ticker", "max": 2}),
            PlanTask("after", "account.risk", depends_on=("each",)),
        ),
        budget=BudgetClass.PORTFOLIO,
    )
    outcome = ExecutionEngine(registry).run(plan, ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO))
    subjects = [result.subject for result in outcome.results]
    # Three holdings under a cap of two: the engine records what it skipped.
    assert subjects == ["portfolio", "each", "NVDA", "AMD", "portfolio"]
    note = outcome.results[1]
    assert note.status == ResultStatus.PARTIAL_DATA and "未覆盖：CRWD" in note.limitations[0]
    assert outcome.ledger.ids() == {"P", "fan-out-coverage-each", "R-NVDA", "R-AMD", "K"}
    empty = ExecutionPlan("q", RouteKind.RESEARCH, tasks=(PlanTask("risk", "account.risk"), PlanTask("each", "research.stock", {"focus": "risk"}, depends_on=("risk",), fan_out={"from": "risk", "argument": "ticker"})), budget=BudgetClass.FOCUSED)
    outcome = ExecutionEngine(registry).run(empty, ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.FOCUSED))
    assert outcome.results[1].status == ResultStatus.SKIPPED
    bad = ExecutionPlan("q", RouteKind.RESEARCH, tasks=(PlanTask("each", "research.stock", {}, fan_out={"from": "missing", "argument": "ticker"}),))
    with pytest.raises(PlanValidationError):
        ExecutionEngine(registry).run(bad, ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.DIRECT))


def test_llm_planner_accepts_fan_out_tasks_and_adds_the_source_dependency():
    rows = [
        {"id": "t1", "capability": "account.portfolio", "arguments": {}},
        {"id": "t2", "capability": "research.stock", "arguments": {"focus": "filings"}, "fan_out": {"from": "t1", "argument": "ticker"}},
    ]
    llm = ScriptedLLM([LLMResponse(text=json.dumps({"tasks": rows}))])
    request = normalize_request("研究一下我持仓里每只的 SEC 申报")
    plan = StructuredLLMPlanner(llm, default_catalog()).plan(request, route(request))
    assert plan.tasks[1].fan_out == {"from": "t1", "field": "tickers", "argument": "ticker", "max": 8}
    assert plan.tasks[1].depends_on == ("t1",)


def test_ledger_accepts_the_same_fact_from_another_run_but_rejects_a_different_claim():
    from v2.agent_v2.evidence import EvidenceConflictError, EvidenceLedger

    ledger = EvidenceLedger()
    first = EvidenceItem("evidence-1", "NVDA", "Revenue growth is +55.3%.", metric="revenue_growth", value=0.553, producer_run_id="run-a", metadata={"snapshot": "a"})
    ledger.add(first)
    ledger.add(EvidenceItem("evidence-1", "NVDA", "Revenue growth is +55.3%.", metric="revenue_growth", value=0.553, producer_run_id="run-b", metadata={"snapshot": "b"}))
    assert ledger.get("evidence-1").producer_run_id == "run-a"
    reissued = ledger.add(EvidenceItem("evidence-1", "NVDA", "Revenue growth is +12.0%.", metric="revenue_growth", value=0.12, producer_run_id="run-c"))
    assert reissued.id == "evidence-1~run-c" and reissued.metadata["original_evidence_id"] == "evidence-1"
    assert ledger.get("evidence-1").claim == "Revenue growth is +55.3%." and ledger.get("evidence-1~run-c").claim == "Revenue growth is +12.0%."
    assert ledger.reissued == [("evidence-1", "evidence-1~run-c")]
    assert isinstance(EvidenceConflictError(), ValueError)


def test_result_level_citation_caps_count_each_results_own_evidence():
    first = [EvidenceItem("A1", "NVDA", "candidate a", metadata={"claim_role": "candidate_driver"})]
    second = [EvidenceItem("B1", "AMD", "candidate b", metadata={"claim_role": "candidate_driver"})]
    cap = {"max_cited": {"metadata": {"claim_role": "candidate_driver"}, "max": 1, "warning": "过多弱候选线索"}}
    results = [
        ToolEnvelope("market.explain_move", ResultStatus.COMPLETED, subject="NVDA", evidence=first, metadata={"answer_constraints": [cap]}),
        ToolEnvelope("market.explain_move", ResultStatus.COMPLETED, subject="AMD", evidence=second, metadata={"answer_constraints": [cap]}),
    ]
    report = verify_answer("NVDA 可能与线索 a 相关。[A1] AMD 可能与线索 b 相关。[B1]", [*first, *second], answer_mode=AnswerMode.RESEARCH_GROUNDED, results=results)
    assert report.ok, report.warnings


def test_llm_planner_extends_thin_fast_lookup_plans_without_dropping_the_scope_read():
    response = LLMResponse(text='{"tasks":[{"id":"t1","capability":"account.performance","arguments":{"period":"month"}}]}')
    catalog = default_catalog()
    request = normalize_request("我这个月比上个月表现好还是差？")
    llm = ScriptedLLM([response])
    plan = StructuredLLMPlanner(llm, catalog).plan(request, route(request))
    assert llm.calls and [task.capability for task in plan.tasks] == ["account.portfolio", "account.performance"]
    request = normalize_request("我的持仓有哪些？")
    llm = ScriptedLLM([LLMResponse(text=response.text)])
    plan = StructuredLLMPlanner(llm, catalog).plan(request, route(request))
    assert [task.capability for task in plan.tasks] == ["account.portfolio", "account.performance"]
    request = normalize_request("AMD最近表现如何？")
    llm = ScriptedLLM([LLMResponse(text=response.text)])
    plan = StructuredLLMPlanner(llm, catalog).plan(request, route(request))
    assert not llm.calls and plan.tasks[0].capability == "market.performance"


def test_benchmark_fixture_makes_a_failed_card_citeable():
    from v2.agent_v2.eval.benchmark_fixtures import build_benchmark_registry

    registry, _ = build_benchmark_registry()
    agent = AgentV2(catalog=registry.catalog, registry=registry)
    result = agent.run("SMCI 最近有什么 8-K")
    assert result.results[0].status == ResultStatus.PARTIAL_DATA and "timed out" in result.results[0].limitations[0]
    assert any(item.metadata.get("citation_kind") == "limitations" for item in result.evidence)
    assert result.verification.ok




def test_legacy_wrap_strips_card_html_and_parses_positions():
    from v2.agent_v2.adapters.legacy import _wrap

    envelope = _wrap("account.portfolio", "portfolio", _PORTFOLIO_CARD)
    assert "<b>" not in envelope.summary and "<code>" not in envelope.evidence[0].claim
    assert envelope.metadata["tickers"] == ["IVV", "BRK.B", "ARM", "MRVL"]
    rows = {row["ticker"]: row for row in envelope.metadata["positions"]}
    assert rows["ARM"]["pl_pct"] == -32.22 and rows["ARM"]["pl"] == -628.0 and rows["ARM"]["pl_pct_text"] == "-32.22%"
    assert rows["IVV"]["pl"] == 1073.0 and rows["IVV"]["market_value"] == 53951.0
    assert envelope.metrics["positions"][0]["ticker"] == "IVV"
    assert envelope.metadata["rankable"][0]["field"] == "pl_pct"


def test_fallback_synthesizer_answers_a_ranking_question_from_the_position_table():
    from v2.agent_v2.adapters.legacy import _wrap

    portfolio = _wrap("account.portfolio", "portfolio", _PORTFOLIO_CARD)
    request = normalize_request("我的仓库里哪只跌的最多?")
    plan = ExecutionPlan(request.text, RouteKind.RESEARCH, tasks=(PlanTask("p", "account.portfolio"),), answer_mode=AnswerMode.TOOL_GROUNDED)
    answer = EvidenceSummarySynthesizer().synthesize(request, plan, [portfolio], portfolio.evidence)
    assert answer == f"按买入以来的浮动盈亏排序，最低的是 ARM（-32.22%），其次是 MRVL（-27.33%）、BRK.B（-0.17%）[{portfolio.evidence[0].id}]。"
    assert "组合价值" not in answer  # the card stays in the evidence list, not the answer
    assert verify_answer(answer, portfolio.evidence, answer_mode=AnswerMode.TOOL_GROUNDED, results=[portfolio]).ok
    winners = EvidenceSummarySynthesizer().synthesize(normalize_request("持仓里哪只赚得最多"), plan, [portfolio], portfolio.evidence)
    assert winners.startswith("按买入以来的浮动盈亏排序，最高的是 IVV（+2.03%）")
    by_size = EvidenceSummarySynthesizer().synthesize(normalize_request("哪只仓位最大"), plan, [portfolio], portfolio.evidence)
    assert by_size.startswith("按市值排序，最高的是 IVV（$53,951）")
    plain = EvidenceSummarySynthesizer().synthesize(normalize_request("看看我的持仓"), plan, [portfolio], portfolio.evidence)
    assert not plain.startswith("按")
    # "仓位" names the size rule, but the direction word belongs to P/L: fall through to it.
    mixed = EvidenceSummarySynthesizer().synthesize(normalize_request("仓位里哪只跌得最多"), plan, [portfolio], portfolio.evidence)
    assert mixed.startswith("按买入以来的浮动盈亏排序，最低的是 ARM（-32.22%）")


def test_executor_orders_a_ranked_fan_out_by_the_source_table_and_discloses_the_cut():
    from v2.agent_v2.adapters.legacy import _wrap

    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)
    registry.register("account.portfolio", lambda a, c: _wrap("account.portfolio", "portfolio", _PORTFOLIO_CARD))
    registry.register("market.explain_move", lambda a, c: ToolEnvelope("market.explain_move", ResultStatus.COMPLETED, subject=a["ticker"], summary=f"{a['ticker']} moved", evidence=[EvidenceItem(f"M-{a['ticker']}", a["ticker"], f"{a['ticker']} moved")]))
    plan = ExecutionPlan(
        "q",
        RouteKind.RESEARCH,
        tasks=(
            PlanTask("holdings", "account.portfolio"),
            PlanTask("each", "market.explain_move", {}, depends_on=("holdings",), fan_out={"from": "holdings", "field": "tickers", "argument": "ticker", "max": 2, "rank": {"field": "positions", "key": "pl_pct", "descending": False}}),
        ),
        budget=BudgetClass.PORTFOLIO,
    )
    outcome = ExecutionEngine(registry).run(plan, ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.PORTFOLIO))
    subjects = [result.subject for result in outcome.results]
    assert subjects == ["portfolio", "each", "ARM", "MRVL"]
    note = outcome.results[1]
    assert "按相关性排序后" in note.limitations[0] and "未覆盖：BRK.B, IVV" in note.limitations[0]
    assert note.evidence[0].metadata["citation_kind"] == "limitations"
    # The ranking answer: conclusion, one line per named holding, what was not covered.
    request = normalize_request("我持仓里今天哪只跌得最多")
    answer = EvidenceSummarySynthesizer().synthesize(request, plan, outcome.results, outcome.ledger.items())
    lines = answer.split("\n")
    assert lines[0].startswith("按买入以来的浮动盈亏排序，最低的是 ARM（-32.22%）")
    assert lines[1:3] == ["ARM moved [M-ARM]", "MRVL moved [M-MRVL]"]
    assert lines[-1] == "market.explain_move 未覆盖：BRK.B、IVV [fan-out-coverage-each]。"
    assert "组合价值" not in answer and len(lines) == 4
    assert verify_answer(answer, outcome.ledger.items(), answer_mode=AnswerMode.TOOL_GROUNDED, results=outcome.results).ok
    # A compound question keeps the other results it asked for.
    risk = ToolEnvelope("account.risk", ResultStatus.COMPLETED, subject="portfolio", summary="集中度 54.7%", evidence=[EvidenceItem("K", "portfolio", "集中度 54.7%")])
    compound = EvidenceSummarySynthesizer().synthesize(normalize_request("我持仓里今天哪只跌得最多，组合风险怎么样"), plan, [*outcome.results, risk], [*outcome.ledger.items(), *risk.evidence])
    assert compound.startswith(answer) and compound.endswith("集中度 54.7% [K]")
    bad = ExecutionPlan("q", RouteKind.RESEARCH, tasks=(PlanTask("h", "account.portfolio"), PlanTask("e", "market.explain_move", {}, depends_on=("h",), fan_out={"from": "h", "argument": "ticker", "rank": {"field": "positions"}})))
    with pytest.raises(PlanValidationError):
        ExecutionEngine(registry).run(bad, ExecutionContext("run", NormalizedRequest("q", "q"), BudgetClass.DIRECT))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我的仓库里今天哪只跌的最多?", {"field": "positions", "key": "pl_pct", "descending": False}),
        ("我持仓里最近哪只涨得最多", {"field": "positions", "key": "pl_pct", "descending": True}),
        ("我持仓里跌得最狠的那只是什么原因", {"field": "positions", "key": "pl_pct", "descending": False}),
        ("我持仓里每只最近怎么样", None),
    ],
)
def test_rule_planner_ranks_portfolio_fan_out_by_direction(query, expected):
    request = normalize_request(query)
    plan = RulePlanner().plan(request, route(request))
    template = next(task for task in plan.tasks if task.fan_out)
    assert template.fan_out.get("rank") == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我的仓库里哪只跌的最多?", ["account.portfolio"]),
        ("我持仓里哪只跌得最多", ["account.portfolio"]),
        ("持仓里谁赚得最多", ["account.performance", "account.portfolio"]),
    ],
)
def test_rule_planner_answers_a_portfolio_ranking_from_the_card_alone(query, expected):
    plan = _plan(query)
    assert [task.capability for task in plan.tasks] == expected
    assert not any(task.fan_out for task in plan.tasks)
    llm = ScriptedLLM([LLMResponse(text="{}")])
    request = normalize_request(query)
    assert [task.capability for task in StructuredLLMPlanner(llm, default_catalog()).plan(request, route(request)).tasks] == expected
    assert llm.calls == []  # the rules own it; the model is not consulted


def test_llm_planner_inherits_the_rules_fan_out_rank():
    rows = [
        {"id": "t1", "capability": "account.portfolio", "arguments": {}},
        {"id": "t2", "capability": "market.performance", "arguments": {}, "fan_out": {"from": "t1", "argument": "ticker"}},
    ]
    llm = ScriptedLLM([LLMResponse(text=json.dumps({"tasks": rows}))])
    request = normalize_request("帮我研究一下我持仓里跌得最多的几只，财报和估值怎么样")
    plan = StructuredLLMPlanner(llm, default_catalog()).plan(request, route(request))
    assert len(llm.calls) == 1
    template = next(task for task in plan.tasks if task.fan_out)
    assert template.fan_out["rank"] == {"field": "positions", "key": "pl_pct", "descending": False}


def test_llm_synthesizer_reports_each_verification_attempt_per_thread():
    import threading

    result = _performance_envelope()
    volatility = next(item for item in result.evidence if item.metadata["evidence_scope"] == "volatility")
    llm = ScriptedLLM([LLMResponse(text=f"AMD 波动率为 99%。[{volatility.id}]"), LLMResponse(text=f"AMD 波动率为 98%。[{volatility.id}]")])
    request = normalize_request("AMD最近表现如何？")
    plan = ExecutionPlan(request.text, RouteKind.FAST_LOOKUP, tasks=(PlanTask("p", "market.performance", {"ticker": "AMD"}),), answer_mode=AnswerMode.TOOL_GROUNDED)
    synthesizer = LLMEvidenceSynthesizer(llm)
    synthesizer.synthesize(request, plan, [result], result.evidence)
    diagnostics = synthesizer.diagnostics()
    assert diagnostics["outcome"] == "fallback" and "99%" in diagnostics["draft"]
    assert [attempt["stage"] for attempt in diagnostics["attempts"]] == ["draft", "repair"]
    assert not diagnostics["attempts"][0]["ok"] and diagnostics["attempts"][0]["warnings"]
    seen: dict[str, str] = {}

    def other_thread() -> None:
        seen["outcome"] = synthesizer.last_outcome

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()
    assert seen["outcome"] == ""  # another thread's run never sees this one's diagnostics


def test_agent_result_carries_synthesis_diagnostics():
    registry = CapabilityRegistry(default_catalog())
    registry.register("account.portfolio", lambda a, c: ToolEnvelope("account.portfolio", ResultStatus.COMPLETED, subject="portfolio", summary="holdings", evidence=[EvidenceItem("P", "portfolio", "holdings")]))
    agent = AgentV2(catalog=registry.catalog, registry=registry)
    payload = agent.run("我的持仓").to_dict()
    assert payload["synthesis"] == {"outcome": "deterministic", "draft": "", "attempts": []}


def test_yfinance_price_source_uses_the_dash_share_class_spelling():
    from v2.data.price_source import YFinancePriceSource

    requested: list[str] = []

    class _Ticker:
        def history(self, **kwargs):
            return None

    def factory(symbol: str):
        requested.append(symbol)
        return _Ticker()

    YFinancePriceSource(ticker_factory=factory).get_prices("BRK.B", "2026-01-01", "2026-01-10")
    assert requested == ["BRK-B"]
    assert YFinancePriceSource.yfinance_symbol("nvda") == "NVDA"


def test_repair_instruction_points_at_the_evidence_that_carries_each_number():
    from v2.agent_v2.llm import repair_instruction
    from v2.agent_v2.models import VerificationReport

    peak = EvidenceItem("D-peak", "ARM", "ARM 从 2026-06-18 的高点 439.46 美元到 2026-07-29 的低点 224.89 美元回撤 -48.83%。", value=-0.4883)
    price = EvidenceItem("AT-price", "ARM", "ARM 在 2026-07-29 收于 224.89 美元，较前一交易日 -8.11%。")
    report = VerificationReport(ok=False, ungrounded_numbers=("439.46", "224.89", "12.34"))
    text = repair_instruction(report, [peak, price])
    assert "439.46 见 [D-peak]；224.89 见 [D-peak]、[AT-price]" in text
    assert "以下数字在本轮证据中找不到：12.34。" in text and "439.46" not in text.split("找不到")[1]
    assert repair_instruction(report).count("找不到：439.46、224.89、12.34") == 1  # without evidence, the old wording
    # A sentence that cited the wrong item reports its figures as a warning; those get the same hint.
    nearby = VerificationReport(ok=False, warnings=("引用未支持邻近数字：439.46, 224.89, -48.8", "行情事实缺少邻近引用"))
    text = repair_instruction(nearby, [peak, price])
    assert "439.46 见 [D-peak]；224.89 见 [D-peak]、[AT-price]；-48.8 见 [D-peak]" in text
    assert "其他问题：行情事实缺少邻近引用。" in text and "其他问题：引用未支持" not in text


def test_attributor_lead_text_keeps_a_quoted_lead_in_one_sentence():
    from v2.agent_v2.agents.move_attributor import lead_text

    lead = lead_text("当日 ARM 大跌主要受芯片股抛售拖累。其 2026 年已累计上涨 235%，市盈率 431 倍。 获利了结压力放大跌幅。")
    assert lead == "当日 ARM 大跌主要受芯片股抛售拖累；其 2026 年已累计上涨 235%，市盈率 431 倍；获利了结压力放大跌幅"
    # A figure-bearing lead quoted inside one cited sentence keeps its citation.
    item = EvidenceItem("lead-1", "ARM", lead, metadata={"claim_role": "candidate_driver"})
    sentence = f"最相关的一条候选线索是“{lead}”，只能作为排查方向[lead-1]。"
    report = verify_answer(sentence, [item], answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[ToolEnvelope("market.attribute_move", ResultStatus.COMPLETED, evidence=[item], metadata={"require_cited_numbers": True})])
    assert report.ok, report


def _drawdown_citation_fixture():
    peak = EvidenceItem("D-peak", "ARM", "ARM 从 2026-06-18 的高点 439.46 美元到 2026-07-29 的低点 224.89 美元回撤 -48.83%。", value=-0.4883)
    price = EvidenceItem("AT-price", "ARM", "ARM 在 2026-07-29 收于 224.89 美元，较前一交易日 -8.11%。", value=-0.0811)
    perf = EvidenceItem("W-ARM", "ARM", "ARM 区间回报：1d +1.03%，5d +12.52%，1m -1.35%，3m -18.66%，1y +89.90%。")
    bench = EvidenceItem("B-SMH", "ARM", "同期基准 SMH 回报：1d +0.10%（ARM 相对 +0.93%），5d +5.33%（ARM 相对 +7.19%）。")
    hidden = EvidenceItem("H-1", "ARM", "内部：ARM 目标价 439.46。", metadata={"citable": False})
    evidence = [peak, price, perf, bench, hidden]
    results = [ToolEnvelope("market.drawdown", ResultStatus.COMPLETED, subject="ARM", evidence=evidence, metadata={"require_cited_numbers": True})]
    return evidence, results


def test_complete_citations_adds_the_one_item_that_carries_a_misattributed_figure():
    from v2.agent_v2.verification import complete_citations

    evidence, results = _drawdown_citation_fixture()
    draft = "ARM 从高点 439.46 美元跌到低点 224.89 美元，回撤 -48.8%[AT-price]。\n近 5 日 +12.52%，跑赢 SMH[B-SMH]。 三只合计 -1,335 美元[AT-price]。"
    completed, notes = complete_citations(draft, evidence, results)
    # The ids go next to the existing citation, before the closing punctuation.
    assert completed.split("\n")[0] == "ARM 从高点 439.46 美元跌到低点 224.89 美元，回撤 -48.8%[AT-price][D-peak]。"
    assert "跑赢 SMH[B-SMH][W-ARM]。" in completed
    # A figure the model computed itself has no carrier and is left for the repair round.
    assert "三只合计 -1,335 美元[AT-price]。" in completed and notes == ["439.46 → [D-peak]", "12.52 → [W-ARM]"]
    report = verify_answer(completed, evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=results)
    assert report.warnings == ("引用未支持邻近数字：-1,335",)
    # Vague figures and figures several items carry are not completed.
    untouched = "ARM 跌了 3 天，2026 年表现[B-SMH]。 收于 224.89 美元[B-SMH]。"
    assert complete_citations(untouched, evidence, results) == (untouched, [])
    assert complete_citations("", evidence, results) == ("", [])


def test_llm_synthesizer_completes_citations_before_verifying_a_draft():
    evidence, results = _drawdown_citation_fixture()
    request = normalize_request("ARM 为什么跌这么多")
    plan = ExecutionPlan("ARM 为什么跌这么多", RouteKind.RESEARCH, answer_mode=AnswerMode.RESEARCH_GROUNDED)
    llm = ScriptedLLM([LLMResponse(text="ARM 从高点 439.46 美元跌到低点 224.89 美元，回撤 -48.8%[AT-price]。近 5 日 +12.52%[B-SMH]。")])
    synthesizer = LLMEvidenceSynthesizer(llm)
    answer = synthesizer.synthesize(request, plan, results, evidence)
    assert answer == "ARM 从高点 439.46 美元跌到低点 224.89 美元，回撤 -48.8%[AT-price][D-peak]。近 5 日 +12.52%[B-SMH][W-ARM]。"
    assert len(llm.calls) == 1  # no repair round was needed
    diagnostics = synthesizer.diagnostics()
    assert diagnostics["outcome"] == "clean" and diagnostics["citation_completions"] == ["439.46 → [D-peak]", "12.52 → [W-ARM]"]
    assert verify_answer(answer, evidence, answer_mode=plan.answer_mode, results=results).ok
