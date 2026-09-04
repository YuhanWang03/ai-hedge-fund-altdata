"""Tests for the evaluation harness — including tests of the answer key itself.

An eval suite is code that judges other code, so a bug in it is worse than a bug
in what it measures: it produces a number that looks authoritative and is wrong.
Three things are checked here.

* **The scorers**, on hand-built inputs where the right answer is obvious.
* **The runner**, so a crashing case is recorded as a failure rather than taking
  the sweep down with it.
* **The answer key**, mechanically. Every fact a case asserts must be findable
  somewhere in the recorded fixtures — otherwise the case is unpassable by
  construction and the suite quietly under-reports every mode equally, which is
  the hardest kind of eval bug to notice.
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent.eval import runner  # noqa: E402
from v2.agent.eval.cases import CASES, CATEGORIES, CLASSIFIER_EXTRAS  # noqa: E402
from v2.agent.eval.fixtures import EVAL_FIXTURES, build_eval_registry  # noqa: E402
from v2.agent.eval.scoring import (  # noqa: E402
    SuiteReport, fact_present, normalise, score_case,
)
from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall  # noqa: E402
from v2.agent.registry import SPECS_BY_NAME  # noqa: E402

_CASE_BY_ID = {c.id: c for c in CASES}


# ---------------------------------------------------------------------------
# fact matching
# ---------------------------------------------------------------------------

def test_thousands_separators_do_not_decide_correctness():
    assert fact_present(("184,320.55",), "总市值 184320.55 美元")
    assert fact_present(("184320.55",), "总市值 $184,320.55")


def test_matching_is_case_insensitive_and_whitespace_tolerant():
    assert fact_present(("Vanguard",), "机构里 VANGUARD 最大")
    assert normalise("a   b\nc") == "a b c"


def test_any_acceptable_form_counts():
    forms = ("2026-09-06", "9月6日", "9/6")
    assert fact_present(forms, "财报在 9月6日")
    assert not fact_present(forms, "财报在 9月9日")


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def _case(cid: str = "m01"):
    return _CASE_BY_ID[cid]


def test_extra_tools_do_not_hurt_recall_but_show_up_as_cost():
    case = _case("m01")
    score = score_case(case, mode="agent", answer="CRWD SMCI 2026-09-06",
                       tools_called=["portfolio_view", "macro_view", "risk_view"],
                       tool_calls=3)
    assert score.tool_recall == 1.0, "多调工具不扣召回"
    assert score.tool_calls == 3


def test_missing_a_required_tool_is_reported_by_name():
    score = score_case(_case("m02"), mode="agent", answer="CRWD 3 次",
                       tools_called=["insider_view"])
    assert score.tool_recall == 0.5
    assert score.missing_tools == ("portfolio_view",)
    assert "portfolio_view" in score.failure_reason()


def test_a_forbidden_tool_fails_the_case_outright():
    case = _CASE_BY_ID["t01"]
    score = score_case(case, mode="agent", answer="总市值 184,320.55",
                       tools_called=["portfolio_view", "risk_view"], tool_calls=2)
    assert score.violations == ("risk_view",)
    assert not score.passed


def test_borrowing_another_tickers_number_is_caught():
    """h01: GOOGL has no flow data; quoting SMCI's is the failure to detect."""
    case = _CASE_BY_ID["h01"]
    score = score_case(case, mode="agent",
                       answer="GOOGL 的 CMF(20) -0.31，资金流出",
                       tools_called=["moneyflow_view"])
    assert score.forbidden_hit and not score.passed


def test_ungrounded_answers_are_not_correct_answers():
    score = score_case(_case("m01"), mode="agent",
                       answer="CRWD SMCI 2026-09-06",
                       tools_called=["portfolio_view"], grounded=False)
    assert score.tool_recall == 1.0 and score.fact_recall == 1.0
    assert not score.passed and score.failure_reason().startswith("数字无法溯源")


def test_ungrounded_figures_are_named_in_the_failure_reason():
    """'数字无法溯源' without saying which number is not an actionable verdict."""
    score = score_case(_case("m01"), mode="agent", answer="CRWD SMCI 2026-09-06",
                       tools_called=["portfolio_view"], grounded=False,
                       ungrounded=("37.9", "12.4"))
    reason = score.failure_reason()
    assert "37.9" in reason and "12.4" in reason


def test_overspend_is_tracked_without_failing_the_case():
    case = _CASE_BY_ID["s01"]          # max_tool_calls=2
    score = score_case(case, mode="agent", answer="今日 3.85%，同期 SMH 上涨",
                       tools_called=["explain_move"], tool_calls=5)
    assert score.overspend and score.passed, "超预算是成本问题，不是正确性问题"


def test_report_aggregates_and_prices_each_pass():
    report = SuiteReport(mode="x", scores=[
        score_case(_case("m01"), mode="x", answer="CRWD SMCI 2026-09-06",
                   tools_called=["portfolio_view"], tokens=1000),
        score_case(_case("m02"), mode="x", answer="没查到",
                   tools_called=[], tokens=3000),
    ])
    assert report.passed == 1 and report.pass_rate == 0.5
    assert report.summary()["tokens_per_pass"] == 4000
    assert [s.case_id for s in report.failures()] == ["m02"]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def test_baseline_leg_needs_no_model_and_is_deterministic():
    llm = ScriptedLLM([])
    first = runner.run_case(_CASE_BY_ID["s06"], runner.MODES["baseline"],
                            llm_factory=lambda: llm)
    second = runner.run_case(_CASE_BY_ID["s06"], runner.MODES["baseline"],
                             llm_factory=lambda: llm)
    assert first.passed and second.passed
    assert llm.calls == [], "基线档不该发生任何模型调用"
    assert first.tokens == second.tokens == 0


def test_agent_leg_is_scored_from_its_trajectory():
    def factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")],
                        prompt_tokens=500, completion_tokens=50),
            LLMResponse(text="CRWD 22.4% 是第一大持仓。",
                        prompt_tokens=800, completion_tokens=60),
        ])

    score = runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"],
                            llm_factory=factory)
    assert score.passed
    assert score.tool_calls == 1 and score.tokens == 1410
    assert score.path == "agent"


def test_routed_mode_sends_a_single_hop_case_down_the_cheap_path():
    llm = ScriptedLLM([])
    score = runner.run_case(_CASE_BY_ID["t01"], runner.MODES["routed"],
                            llm_factory=lambda: llm)
    assert score.path == "single_hop"
    assert llm.calls == [], "路由判定为单跳时不该起 agent"
    assert score.passed


def test_routed_mode_escalates_a_ranking_case():
    def factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
            LLMResponse(text="SMCI 亏 21.5% 最多。"),
        ])

    score = runner.run_case(_CASE_BY_ID["r02"], runner.MODES["routed"],
                            llm_factory=factory)
    assert score.path == "agent" and score.passed


def test_a_crashing_case_is_a_failure_not_a_dead_sweep():
    class Exploding:
        def complete(self, messages, tools=None):
            raise RuntimeError("simulated crash")   # escapes run_agent's guard

    score = runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"],
                            llm_factory=lambda: Exploding())
    assert not score.passed and "RuntimeError" in score.error


def test_ctrl_c_still_stops_the_sweep():
    """A crashing case is data; an interrupt is the operator, and must get through."""
    class Interrupted:
        def complete(self, messages, tools=None):
            raise KeyboardInterrupt

    try:
        runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"],
                        llm_factory=lambda: Interrupted())
    except KeyboardInterrupt:
        return
    raise AssertionError("KeyboardInterrupt 被吞掉了，Ctrl-C 将无法中止长跑")


def test_suite_runs_every_case_once():
    report = runner.run_suite("baseline", llm_factory=lambda: ScriptedLLM([]),
                              cases=CASES[:10], workers=1)
    assert len(report.scores) == 10
    assert len({s.case_id for s in report.scores}) == 10


# ---------------------------------------------------------------------------
# the answer key itself
# ---------------------------------------------------------------------------

def _fixture_corpus() -> str:
    """Everything the recorded tool layer can ever say."""
    chunks: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif callable(value):
            for ticker in ("CRWD", "NVDA", "ARM", "AVGO", "TSLA", "AMD",
                           "AAPL", "MSFT", "GOOGL", "PLTR"):
                try:
                    chunks.append(value({"ticker": ticker}))
                except Exception:
                    pass          # SMCI's simulated timeout is intentional

    walk(EVAL_FIXTURES)
    return normalise("\n".join(chunks))


def test_every_asserted_fact_exists_in_the_fixtures():
    """A case asserting a fact no tool can produce is unpassable by construction."""
    corpus = _fixture_corpus()
    unreachable = []
    for case in CASES:
        for fact in case.facts:
            if not any(normalise(form) in corpus for form in fact):
                unreachable.append(f"{case.id}: {fact[0]}")
    assert not unreachable, "标注了 fixture 里根本不存在的事实：\n" + "\n".join(unreachable)


def test_every_required_tool_exists():
    unknown = []
    for case in CASES:
        for tool in case.must_call + case.must_not_call:
            if tool not in SPECS_BY_NAME:
                unknown.append(f"{case.id}: {tool}")
    assert not unknown, "引用了不存在的工具：" + ", ".join(unknown)


def test_case_ids_are_unique_and_categories_known():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    assert {c.category for c in CASES} <= set(CATEGORIES)
    assert {c.expected_path for c in CASES} <= {"single_hop", "agent", "slash"}


def test_classifier_extras_reference_real_cases_and_fields():
    known_fields = {"manager", "etf", "release_type", "period", "days_horizon",
                    "days_back", "target_price", "direction"}
    for case_id, extra in CLASSIFIER_EXTRAS.items():
        assert case_id in _CASE_BY_ID, case_id
        assert set(extra) <= known_fields, f"{case_id}: {set(extra) - known_fields}"


def test_registry_can_serve_every_single_hop_case():
    """The fast path must actually reach a tool for each case labelled single_hop."""
    registry = build_eval_registry()
    broken = []
    for case in CASES:
        if case.expected_path != "single_hop" or not case.must_call:
            continue
        tool = case.must_call[0]
        args = {}
        spec = SPECS_BY_NAME[tool]
        props = spec.parameters.get("properties", {})
        if "ticker" in props and case.ticker:
            args["ticker"] = case.ticker
        for key, value in case.extra.items():
            if key in props:
                args[key] = value
        if "symbol" in props and case.extra.get("etf"):
            args["symbol"] = case.extra["etf"]
        result = registry.call(tool, args)
        if not result.ok and case.category != "recovery":
            broken.append(f"{case.id}/{tool}: {result.error_kind}")
    assert not broken, "单跳 case 无法取到数据：" + ", ".join(broken)


if __name__ == "__main__":
    import traceback

    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception:
            failures += 1
            print(f"  ✗ {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
