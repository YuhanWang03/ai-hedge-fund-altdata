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
    assert result.status == RunStatus.FAILED
    assert "证据标识" in result.answer
    assert "conflicting evidence id" in result.error
    assert not result.verification.ok
    assert result.verification.warnings == ("证据完整性检查失败",)


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


def test_agent_v2_seed_eval_passes_offline():
    report = run_suite()
    assert report.passed == report.total


def test_telegram_ask_v2_command_is_explicit_and_parses_web_consent(monkeypatch):
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
        args = ["--web", "比较", "NVDA", "和", "AMD"]

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    monkeypatch.setattr(agent_v2_bridge, "handle_agent_v2", handle)
    asyncio.run(commands.cmd_agent_v2(Update(), Context()))
    assert called == {"text": "比较 NVDA 和 AMD", "allow_web": True}


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
    assert subjects == ["portfolio", "NVDA", "AMD", "portfolio"]
    assert outcome.ledger.ids() == {"P", "R-NVDA", "R-AMD", "K"}
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
    assert plan.tasks[1].fan_out == {"from": "t1", "field": "tickers", "argument": "ticker", "max": 6}
    assert plan.tasks[1].depends_on == ("t1",)
