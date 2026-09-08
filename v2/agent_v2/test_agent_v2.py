"""Contract tests for the initial Agent V2 framework; no API keys required."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from v2.agent.llm import LLMResponse, ScriptedLLM
from v2.agent_v2.adapters.lab import register_lab_capabilities
from v2.agent_v2.adapters.market import register_market_capabilities
from v2.agent_v2.adapters.research import register_research_capabilities
from v2.agent_v2.adapters.tavily_web import TavilyWebSearchPort
from v2.agent_v2.adapters.web import register_web_capability
from v2.agent_v2.adapters.workspace_lab import LabBinding, WorkspaceLabPort
from v2.agent_v2.catalog import default_catalog
from v2.agent_v2.eval.runner import run_suite
from v2.agent_v2.execution import CapabilityRegistry, ExecutionContext, ExecutionEngine
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
    register_market_capabilities(registry, price_source_factory=Prices, move_provider=lambda ticker: None)
    result = registry.execute(PlanTask("performance", "market.performance", {"ticker": "AMD"}), _context())
    assert result.ok
    assert result.metrics["returns"]["5d"] is not None
    assert result.metrics["relative_returns"]["SMH"]["5d"] is not None
    assert {item.metadata["evidence_scope"] for item in result.evidence} >= {"price", "returns", "volume", "benchmark"}


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
    register_market_capabilities(registry, price_source_factory=lambda: None, move_provider=lambda ticker: anomaly)
    result = registry.execute(PlanTask("move", "market.explain_move", {"ticker": "AMD"}), _context())
    scopes = [item.metadata["evidence_scope"] for item in result.evidence]
    assert scopes[:3] == ["price", "volume", "benchmark"]
    assert result.metrics["confirmed_driver_count"] == 1
    assert result.findings[0]["confirmed"] is True
    assert result.findings[1]["confirmed"] is False
    assert next(item for item in result.evidence if item.metadata.get("claim_role") == "candidate_driver").confidence == 0.3


def test_executor_collects_structured_evidence():
    catalog = default_catalog()
    registry = CapabilityRegistry(catalog)

    def research(args, context):
        item = EvidenceItem("E1", args["ticker"], "supported claim")
        return ToolEnvelope("research.stock", ResultStatus.COMPLETED, subject=args["ticker"], summary=item.claim, evidence=[item])

    registry.register("research.stock", research)
    plan = ExecutionPlan("research", RouteKind.RESEARCH, (PlanTask("one", "research.stock", {"ticker": "NVDA"}),), BudgetClass.STANDARD)
    results, ledger = ExecutionEngine(registry).run(plan, _context())
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
                "production_diagnostics": {"modules": {}},
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
    results, ledger = ExecutionEngine(registry).run(plan, _context())
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
    ("query", "intent"),
    [("AMD最近表现如何", "recent_performance"), ("AMD今天是不是涨了，为什么？", "move_explanation")],
)
def test_llm_synthesizer_sets_market_response_intent(query, intent):
    llm = ScriptedLLM([LLMResponse(text="有证据的回答。[E1]")])
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request(query)
    evidence = [EvidenceItem("E1", "AMD", "支持结论")]
    synthesizer.synthesize(request, ExecutionPlan(query, RouteKind.RESEARCH), [], evidence)
    payload = json.loads(llm.calls[0][1]["content"])
    assert payload["response_intent"] == intent


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


def test_llm_synthesizer_keeps_invalid_result_paths_for_verifier_to_reject():
    llm = ScriptedLLM([LLMResponse(text="虚构评分为 96。[results.metrics.scores.invented]")])
    synthesizer = LLMEvidenceSynthesizer(llm)
    request = normalize_request("分析 NVDA")
    plan = ExecutionPlan("分析 NVDA", RouteKind.RESEARCH)
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
    assert "[results.metrics.scores.invented]" in answer
    report = verify_answer(answer, evidence, answer_mode=AnswerMode.RESEARCH_GROUNDED, results=[result])
    assert not report.ok
    assert "results.metrics.scores.invented" in report.unknown_citations


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


def test_verifier_rejects_candidate_driver_written_as_confirmed_cause():
    evidence = [EvidenceItem("C1", "AMD", "低置信度候选解释：期权市场波动。", metadata={"claim_role": "candidate_driver"})]
    report = verify_answer(
        "AMD 上涨的主要原因是期权市场波动。[C1]",
        evidence,
        answer_mode=AnswerMode.RESEARCH_GROUNDED,
    )
    assert not report.ok
    assert any("候选归因" in warning for warning in report.warnings)


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

        def get_result(self, run_id, context):
            return ToolEnvelope("lab.result", ResultStatus.COMPLETED, run_id=run_id)

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
