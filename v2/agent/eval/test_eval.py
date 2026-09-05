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
from v2.agent.eval.holdout import HOLDOUT, HOLDOUT_EXTRAS  # noqa: E402
from v2.agent.eval.fixtures import EVAL_FIXTURES, build_eval_registry  # noqa: E402
from v2.agent.eval.scoring import (  # noqa: E402
    SuiteReport, fact_present, normalise, score_case,
)
from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall  # noqa: E402
from v2.agent.registry import SPECS_BY_NAME  # noqa: E402

_CASE_BY_ID = {c.id: c for c in CASES}
#: The answer-key checks apply to the held-out set too: a label error there is
#: worse, because the number it produces is the one meant to be trusted most.
_ALL_CASES = CASES + HOLDOUT


# ---------------------------------------------------------------------------
# fact matching
# ---------------------------------------------------------------------------

def test_a_date_written_in_chinese_is_the_same_date():
    """gpt-4.1-mini answered 「CRWD 下次财报日期是 2026 年 9 月 6 日」 and was
    scored 事实缺失 against ("2026-09-06", "9-06"). deepseek-chat copies the
    ISO form off the card, so fifteen rounds of labels never noticed they were
    testing spelling. Either spelling on either side must match."""
    assert fact_present(("2026-09-06", "9-06"), "CRWD 下次财报日期是 2026 年 9 月 6 日（盘后）")
    assert fact_present(("2026-10-21", "10-21"), "TSLA下一次财报日期是2026年10月21日")
    assert fact_present(("09-30", "9-30"), "MSFT 9月30日")
    assert fact_present(("9 月 6",), "2026-09-06 (D-3) CRWD")
    # Not a date, not touched: a decimal range, a plan name, a bare figure.
    assert not fact_present(("9-06",), "2026-08-25 Item 5.02")
    assert normalise("维持 4.25-4.50% 不变") == "维持 4.25-4.50% 不变"
    assert normalise("10b5-1 计划") == "10b5-1 计划"
    assert normalise("上周五我亏了多少") == "上周五我亏了多少"


def test_thousands_separators_do_not_decide_correctness():
    assert fact_present(("184,320.55",), "总市值 184320.55 美元")
    assert fact_present(("184320.55",), "总市值 $184,320.55")


def test_matching_is_case_insensitive_and_whitespace_tolerant():
    assert fact_present(("Vanguard",), "机构里 VANGUARD 最大")
    assert normalise("a   b\nc") == "a b c"


def test_chinese_date_spacing_does_not_decide_correctness():
    """A model writing 「9月6日」 must not be scored against the label's spacing."""
    assert fact_present(("2026-09-06", "9 月 6"), "财报在 9月6日 盘后")
    assert fact_present(("9 月 6",), "9月6日")
    assert not fact_present(("9 月 6",), "9月9日")


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
                       tools_called=["earnings_calendar", "macro_view", "risk_view"],
                       tool_calls=3)
    assert score.tool_recall == 1.0, "多调工具不扣召回"
    assert score.tool_calls == 3


def test_missing_a_required_tool_is_reported_by_name():
    score = score_case(_case("m02"), mode="agent", answer="CRWD 3 次",
                       tools_called=["insider_view"])
    assert score.tool_recall == 0.5
    assert score.missing_tools == ("portfolio_view",)
    assert "portfolio_view" in score.failure_reason()


def test_a_redundant_tool_is_charged_to_cost_not_correctness():
    """t02 failed 3/3 for calling one extra tool while answering correctly.

    Waste was being counted twice — once in the cost metrics, once as a hard
    failure — which contradicts the rule ``must_call`` already follows. It is a
    cost signal now, and the case passes on the strength of its answer.
    """
    case = _CASE_BY_ID["t01"]
    score = score_case(case, mode="agent", answer="总市值 184,320.55",
                       tools_called=["portfolio_view", "risk_view"], tool_calls=2)
    assert score.waste == ("risk_view",)
    assert not score.violations
    assert score.passed, "答案正确就该通过；多调工具体现在成本指标里"


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
                       tools_called=["earnings_calendar"], grounded=False)
    assert score.tool_recall == 1.0 and score.fact_recall == 1.0
    assert not score.passed and score.failure_reason().startswith("数字无法溯源")


def test_ungrounded_figures_are_named_in_the_failure_reason():
    """'数字无法溯源' without saying which number is not an actionable verdict."""
    score = score_case(_case("m01"), mode="agent", answer="CRWD SMCI 2026-09-06",
                       tools_called=["earnings_calendar"], grounded=False,
                       ungrounded=("37.9", "12.4"))
    reason = score.failure_reason()
    assert "37.9" in reason and "12.4" in reason


def test_ungrounded_figures_are_classified_by_why_they_failed():
    """Distinguishes 'did not show its arithmetic' from 'made it up'."""
    from v2.agent import grounding

    observations = "CRWD 22.4% · NVDA 18.2% · MSFT 14.1% · 内部人 7.96M / 10.97M"
    report = grounding.GroundingReport(total=3, grounded=0,
                                       ungrounded=["54.7", "22.40", "88.6"])
    kinds = grounding.diagnose(report, observations)

    assert "54.7" in kinds.get("sum", []), "22.4+18.2+14.1"
    assert "22.40" in kinds.get("rounding", [])
    assert "88.6" in kinds.get("unknown", []), "没有任何算术来源 = 编造"


def test_the_diagnoser_does_not_launder_fabrications_as_ratios():
    """Ratios match by coincidence far too easily; attempting them would turn
    invented statistics into 'legitimate arithmetic' and defeat the purpose."""
    from v2.agent import grounding
    assert "ratio" not in grounding.FIGURE_KINDS


def test_overspend_is_tracked_without_failing_the_case():
    case = _CASE_BY_ID["s01"]          # max_tool_calls=2
    score = score_case(case, mode="agent", answer="今日 3.85%，同期 SMH 上涨",
                       tools_called=["explain_move"], tool_calls=5)
    assert score.overspend and score.passed, "超预算是成本问题，不是正确性问题"


def test_report_aggregates_and_prices_each_pass():
    report = SuiteReport(mode="x", scores=[
        score_case(_case("m01"), mode="x", answer="CRWD SMCI 2026-09-06",
                   tools_called=["earnings_calendar"], tokens=1000),
        score_case(_case("m02"), mode="x", answer="没查到",
                   tools_called=[], tokens=3000),
    ])
    assert report.passed == 1 and report.pass_rate == 0.5
    assert report.summary()["tokens_per_pass"] == 4000
    assert [s.case_id for s in report.failures()] == ["m02"]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def test_the_production_mode_measures_what_production_runs():
    """Every sweep before this ran the loop at 20 calls; the bot runs it at 8.
    The overspend being chased was behaviour production never exhibits, and
    「what does the 8-call cap cost」 had never been measured. This mode exists
    to ask that, so its numbers must not drift from the bot's."""
    import os
    from v2.agent import bot_bridge

    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if k.startswith("V2_AGENT_MAX_")}
    try:
        live = bot_bridge.production_config()
    finally:
        os.environ.update(saved)

    mode = runner.MODES["production"]
    assert mode.kind == "routed", "生产走的是路由，不是裸 agent"
    for field in ("max_steps", "max_tool_calls", "max_calls_per_tool", "max_seconds"):
        assert getattr(mode.config, field) == getattr(live, field), field
    assert "production" in runner.DEFAULT_MODES

    # …and the entry script must not keep its own copy of the default list. It
    # did, and a sweep meant to measure this mode ran without it.
    import ast
    source = (pathlib.Path(__file__).resolve().parents[1] / "run_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "MODES" for t in node.targets):
            assert "DEFAULT_MODES" in ast.unparse(node), "run_eval.MODES 必须引用 runner.DEFAULT_MODES"


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


def test_routed_mode_escalates_a_composite_judgment():
    """r01 asks which holding is most dangerous — a verdict no single card gives."""
    def factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}"),
                                    ToolCall("c2", "earnings_view",
                                             {"ticker": "CRWD"},
                                             '{"ticker": "CRWD"}')]),
            LLMResponse(text="CRWD 最危险：占仓 22.4%，2026-09-06 财报。"),
        ])

    score = runner.run_case(_CASE_BY_ID["r01"], runner.MODES["routed"],
                            llm_factory=factory)
    assert score.path == "agent" and score.passed


def test_routed_mode_keeps_a_single_column_ranking_cheap():
    """r02 ranks on a column the positions card already prints."""
    llm = ScriptedLLM([])
    score = runner.run_case(_CASE_BY_ID["r02"], runner.MODES["routed"],
                            llm_factory=lambda: llm)
    assert score.path == "single_hop" and score.passed
    assert llm.calls == []


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


def test_repeats_separate_stable_failures_from_flaky_ones():
    """Two sweeps of the identical config shared only 9 of 25 failures, so a
    single run cannot tell 'broken' from 'unlucky'. This is the mechanism."""
    from v2.agent.eval.scoring import SuiteReport

    case = _CASE_BY_ID["m01"]
    good = score_case(case, mode="agent", answer="CRWD SMCI 2026-09-06",
                      tools_called=["earnings_calendar"])
    bad = score_case(case, mode="agent", answer="没查到",
                     tools_called=["earnings_calendar"])
    other = _CASE_BY_ID["m02"]
    always_bad = score_case(other, mode="agent", answer="", tools_called=[])

    report = SuiteReport(mode="agent", repeat=2,
                         scores=[good, bad, always_bad, always_bad])
    assert report.stability() == {"m01": (1, 2), "m02": (0, 2)}
    assert report.stable_failures() == ["m02"]
    assert report.flaky() == [("m01", 1, 2)]


def test_the_trace_records_the_path_a_run_took_and_not_the_refusals():
    """Six cases stayed flaky at temperature 0. To say *where* two runs of the
    same query fork, each run has to keep its path; a duplicate call that was
    turned away added no evidence and must not appear in it."""
    def factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
            LLMResponse(tool_calls=[ToolCall("c2", "portfolio_view", {}, "{}"),   # dup
                                    ToolCall("c3", "earnings_view",
                                             {"ticker": "CRWD"}, '{"ticker":"CRWD"}')]),
            LLMResponse(text="CRWD 最危险：占仓 22.4%，2026-09-06 财报。"),
        ])

    score = runner.run_case(_CASE_BY_ID["r01"], runner.MODES["agent"], llm_factory=factory)
    assert score.trace == ("portfolio_view()", "earnings_view(ticker=CRWD)")
    assert score.refused_calls == 1
    assert "22.4%" in score.answer


def test_divergence_names_where_the_failing_runs_left_the_passing_path():
    """Pass ratio says a case is flaky; only the fork says which of four places
    to look. The four kinds are ordered: an errored run has a short trace too,
    and must not be misread as a tool-choice fork."""
    from v2.agent.eval.scoring import diverge

    case = _CASE_BY_ID["r01"]
    good_path = ["portfolio_view()", "earnings_view(ticker=CRWD)"]
    good = score_case(case, mode="agent", answer="CRWD 22.4% 2026-09-06",
                      tools_called=["portfolio_view", "earnings_view"],
                      stop_reason="final_answer", trace=good_path)

    wording = score_case(case, mode="agent", answer="CRWD 看起来还行",
                         tools_called=["portfolio_view", "earnings_view"],
                         stop_reason="final_answer", trace=good_path)
    d = diverge([good, good, wording])
    assert d.kind == "wording" and d.fork_at is None
    assert d.good_trace == tuple(good_path) and d.reasons and "事实缺失" in d.reasons[0]

    choice = score_case(case, mode="agent", answer="",
                        tools_called=["portfolio_view", "risk_view"],
                        stop_reason="final_answer",
                        trace=["portfolio_view()", "risk_view()"])
    d = diverge([good, choice])
    assert d.kind == "tool_choice" and d.fork_at == 1
    assert d.bad_trace[1] == "risk_view()"

    budget = score_case(case, mode="agent", answer="",
                        tools_called=["portfolio_view"], stop_reason="budget_exhausted",
                        trace=good_path[:1])
    d = diverge([good, budget])
    assert d.kind == "budget" and d.fork_at == 1

    # Same prefix, but the model *chose* to write: that is not a tool-choice
    # fork (m07 had every must_call tool in hand and still left 7.42% out).
    early = score_case(case, mode="agent", answer="",
                       tools_called=["portfolio_view"], stop_reason="final_answer",
                       trace=good_path[:1])
    d = diverge([good, early])
    assert d.kind == "early_stop" and d.fork_at == 1

    # The provider dropped the *final* call: the path is complete and identical
    # to a passing run, so trace comparison alone would call this "wording".
    error = score_case(case, mode="agent", answer="",
                       tools_called=["portfolio_view", "earnings_view"],
                       stop_reason="llm_error", error="TimeoutError: read timed out",
                       trace=good_path)
    d = diverge([good, error])
    assert d.kind == "error", "报错的运行不管路径长短都是 error，不是措辞或工具选择"
    d = diverge([good, score_case(case, mode="agent", answer="", tools_called=[],
                                  stop_reason="llm_error", error="boom", trace=[])])
    assert d.kind == "error"

    # Majority kind wins; the shown failing run is one of that kind.
    d = diverge([good, wording, wording, choice])
    assert d.kind == "wording"

    report = SuiteReport(mode="agent", repeat=2,
                         scores=[good, wording, good, choice, good, good])
    assert report.flaky_by_kind() == {"wording": ["r01"]} or \
        report.flaky_by_kind() == {"tool_choice": ["r01"]}
    assert report.summary()["flaky_by_kind"]


def test_the_divergence_report_marks_the_fork_and_only_prints_for_repeats():
    case = _CASE_BY_ID["r01"]
    good = score_case(case, mode="agent", answer="CRWD 22.4% 2026-09-06",
                      tools_called=["portfolio_view", "earnings_view"],
                      stop_reason="final_answer",
                      trace=["portfolio_view()", "earnings_view(ticker=CRWD)"])
    bad = score_case(case, mode="agent", answer="", tools_called=["portfolio_view", "risk_view"],
                     stop_reason="final_answer", trace=["portfolio_view()", "risk_view()"])
    single = SuiteReport(mode="agent", repeat=1, scores=[bad])
    assert runner.render_divergence(single) == "", "单轮没有分叉可言"

    text = runner.render_divergence(SuiteReport(mode="agent", repeat=2, scores=[good, bad]))
    assert "r01" in text and "1/2" in text and "工具选择" in text
    assert "⟨risk_view()⟩" in text, "失败路径里第一处不同的调用要被标出来"
    assert "第 2 步分叉" in text

    # The sweep entry point actually prints it — a renderer nobody calls is
    # the production-mode bug again.
    import ast
    source = (_REPO_ROOT / "v2/agent/run_eval.py").read_text(encoding="utf-8")
    calls = {node.func.attr for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "render_divergence" in calls


def test_a_rewrite_that_loses_the_drafts_facts_is_booked_against_the_check():
    """m07's failing run: the draft named ARM +7.42%, a check rejected it, and
    the rewrite refused everything — 「无法确认这些数字分别属于哪一只」. That
    was scored as 事实缺失, a model failure, and the check's own error rate
    stayed at zero. The draft and its findings are now kept, and a rewrite that
    drops facts the draft had is the check's regression, not the model's."""
    import ast

    def factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
            # Draft: the right fact plus one invented figure → grounding repair.
            LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓；组合 beta 1.37。"),
            # Rewrite: over-corrects and drops the fact too.
            LLMResponse(text="数据不足，无法确认第一大持仓。"),
        ])

    score = runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"], llm_factory=factory)
    assert not score.passed and score.repairs == 1
    assert "22.4%" in score.draft and score.draft_facts_ok
    assert score.draft_findings == ("无法溯源 1.37",)
    assert score.repair_regressed
    assert score.failure_reason().startswith("重写丢了事实")

    report = SuiteReport(mode="agent", scores=[score])
    assert report.summary()["repair_regression_rate"] == 1.0
    text = runner.render_repairs(report)
    assert "r03" in text and "1.37" in text
    row = runner.to_json([report])["cases"][0]
    assert row["draft"] == score.draft and row["repair_regressed"] is True

    # p04's shape: the rewrite is the corrected sentence and nothing else.
    def fragment_factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
            LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓。\n\nNVDA 18.2%，MSFT 14.1%。"
                             "\n\n组合 beta 1.37。"),
            LLMResponse(text="组合数据里没有 beta 这一项。"),
        ])
    fragment = runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"],
                               llm_factory=fragment_factory)
    assert fragment.repairs == 1 and fragment.repair_regressed
    assert fragment.partial_rewrite, "初稿溯源过的数字过半不见了，这是只回了一段"
    assert fragment.failure_reason().startswith("重写只回了改动的那一段")
    assert not score.partial_rewrite, "整段改坏和只回一段是两种形状"
    assert "只回了一段" in runner.render_repairs(SuiteReport(mode="agent", scores=[fragment]))

    # A rewrite that keeps the facts but is still ungrounded is the model not
    # complying, not the check regressing it.
    def stubborn_factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
            LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓；组合 beta 1.37。"),
            LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓；组合 beta 1.37。"),
        ])
    stubborn = runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"],
                               llm_factory=stubborn_factory)
    assert not stubborn.passed and stubborn.repairs == 1 and not stubborn.repair_regressed

    # A rewrite that keeps the facts is a repair that worked, not a regression.
    def good_factory():
        return ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
            LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓；组合 beta 1.37。"),
            LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓。"),
        ])
    fixed = runner.run_case(_CASE_BY_ID["r03"], runner.MODES["agent"], llm_factory=good_factory)
    assert fixed.passed and fixed.repairs == 1 and not fixed.repair_regressed
    assert runner.render_repairs(SuiteReport(mode="agent", scores=[fixed])).count("没有重写") == 1

    source = (_REPO_ROOT / "v2/agent/run_eval.py").read_text(encoding="utf-8")
    calls = {node.func.attr for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "render_repairs" in calls


def test_a_saved_sweep_can_be_rescored_against_a_changed_answer_key():
    """The JSON keeps every answer and the tools each run reached, so an
    answer-key change can be measured on runs that already happened instead
    of buying another sweep to find out."""
    from v2.agent.eval import rescore

    case = _CASE_BY_ID["s02"]                       # TSLA 什么时候发财报 → 2026-10-21
    good = score_case(case, mode="routed", answer="TSLA 下次财报 2026-10-21。",
                      tools_called=["earnings_view"], trace=["earnings_view(ticker=TSLA)"])
    bad = score_case(case, mode="routed", answer="TSLA 下次财报在十月底。",
                     tools_called=["earnings_view"], trace=["earnings_view(ticker=TSLA)"])
    report = SuiteReport(mode="routed", scores=[good, bad])
    payload = runner.to_json([report])
    payload["provider"] = "scripted"

    result = rescore.rescore(payload)
    assert result["routed"]["saved"] == 1 and result["routed"]["rescored"] == 1
    assert result["routed"]["changed"] == [], "答案键没变，重打分不该动任何一条"

    # Simulate a key change by editing the saved verdict: the row says it
    # passed, the key says it did not → reported as a flip, not silently kept.
    payload["cases"][1]["passed"] = True
    result = rescore.rescore(payload)
    assert [c[0] for c in result["routed"]["changed"]] == ["s02"]
    text = rescore.render(result, "scripted")
    assert "✓→✗ s02" in text and "1/2" in text


def test_repeat_is_skipped_for_the_deterministic_baseline():
    report = runner.run_suite("baseline", llm_factory=lambda: ScriptedLLM([]),
                              cases=CASES[:5], workers=1, repeat=3)
    assert report.repeat == 1 and len(report.scores) == 5, "基线是确定性的，重复纯属浪费"


def test_suite_runs_every_case_once():
    report = runner.run_suite("baseline", llm_factory=lambda: ScriptedLLM([]),
                              cases=CASES[:10], workers=1)
    assert len(report.scores) == 10
    assert len({s.case_id for s in report.scores}) == 10


# ---------------------------------------------------------------------------
# the answer key itself
# ---------------------------------------------------------------------------

def _fixture_corpus(tools: tuple[str, ...] | None = None) -> str:
    """What the recorded tool layer can say — all of it, or just these tools."""
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

    if tools is None:
        walk(EVAL_FIXTURES)
    else:
        for name in tools:
            walk(EVAL_FIXTURES.get(name))
    return normalise("\n".join(chunks))


def test_every_asserted_fact_comes_from_a_tool_the_case_requires():
    """Stricter than the corpus check below, and it had to be.

    「NVDA 和 AMD 上次财报各自超预期多少」 asserted 27.3% — which is NVDA's
    *unrealised holding gain* in the positions card, not its earnings surprise
    (+5.6%). The corpus check passed it, because 27.3% does exist somewhere in
    the fixtures; the case was unpassable anyway, because nothing the question's
    own tool returns will ever say it. That is the h07 trap this suite already
    documents, made a second time by the person who documented it.

    So a fact must be quotable from a tool the case actually requires. When it
    is not, either the label is wrong or ``must_call`` is missing a tool — r07
    was the second kind, asserting a figure only ``explain_move`` produces.
    """
    unreachable = []
    for case in _ALL_CASES:
        if not case.must_call:
            continue
        corpus = _fixture_corpus(case.must_call)
        for fact in case.facts:
            if not any(normalise(form) in corpus for form in fact):
                unreachable.append(
                    f"{case.id} [{'+'.join(case.must_call)}]: {fact[0]}")
    assert not unreachable, (
        "这些事实不在该用例 must_call 的卡片里 —— 要么标注错了，"
        "要么 must_call 少了一个工具：\n" + "\n".join(unreachable))


def test_every_asserted_fact_exists_in_the_fixtures():
    """A data fact no tool can produce makes the case unpassable by construction.

    Behavioural assertions are exempt by design: "承认这里取不到数据" is a
    property of the answer, not of the fixtures, so requiring it to appear in a
    recorded card would be incoherent.
    """
    corpus = _fixture_corpus()
    unreachable = []
    for case in _ALL_CASES:
        for fact in case.facts:
            if not any(normalise(form) in corpus for form in fact):
                unreachable.append(f"{case.id}: {fact[0]}")
    assert not unreachable, "标注了 fixture 里根本不存在的事实：\n" + "\n".join(unreachable)


def _answer_for(case) -> str:
    """An answer that satisfies every fact the case asserts."""
    return " ".join(forms[0] for forms in tuple(case.facts) + tuple(case.behaviors))


def test_a_warning_on_a_correct_answer_fails_the_case():
    """The axis that did not exist for nine rounds.

    A case whose facts, tools and forbidden strings all check out has, by the
    suite's own definition, put the right numbers against the right companies.
    An attribution warning on top of that is the checker being wrong — and while
    it went unscored, seven such warnings reached production and were found by a
    human reading Telegram instead of by this file.
    """
    case = CASES[0]
    clean = score_case(case, mode="agent", answer=_answer_for(case),
                       tools_called=case.must_call)
    assert clean.passed and not clean.false_misattribution

    flagged = score_case(case, mode="agent", answer=_answer_for(case),
                         tools_called=case.must_call,
                         misattributed=("SMH←-23.6(实为 SMCI)",))
    assert flagged.answer_correct, "回答本身没变"
    assert flagged.false_misattribution, "正确回答上的警告 = 检查器误报"
    assert not flagged.passed, "误报必须让用例失败，否则没人会去看它"

    # A warning on an answer that was already wrong is not the checker's fault,
    # so it must not be counted against the checker's error rate.
    wrong = score_case(case, mode="agent", answer="（空）",
                       tools_called=(), misattributed=("A←1(实为 B)",))
    assert not wrong.answer_correct and not wrong.false_misattribution


def test_checker_stress_cases_aim_at_the_shapes_that_broke_it():
    """These cases exist to be *hard on the checker*, so each one must actually
    reach the fixture region that defeated it — a date column, a threshold, a
    window length. A case whose fixtures lost the awkward shape would pass
    forever while measuring nothing."""
    from v2.agent.eval.fixtures import EVAL_FIXTURES

    stress = [c for c in CASES if c.category == "checker_stress"]
    assert len(stress) >= 6

    calendar = EVAL_FIXTURES["earnings_calendar"]
    assert "10-21" in calendar and "09-30" in calendar, "日期列被改成了不刁钻的形状"
    assert "(D-" in calendar, "倒计时也是同一类陷阱"
    assert "52 周高点" in EVAL_FIXTURES["moneyflow_view"]["MSFT"]
    assert "阈值" in EVAL_FIXTURES["risk_view"], "阈值数字不归任何主体所有"

    for case in stress:
        assert case.note, f"{case.id}: 要写清它针对的是哪一种形状"


def test_no_case_can_pass_without_asserting_anything():
    """A case with no facts and no forbidden strings passes by construction.

    That is worse than a missing case: it silently inflates *every* mode by the
    same amount, so the comparison still looks coherent while being wrong. Six
    of these survived the first draft and were only caught when the baseline
    scored suspiciously well on questions it cannot answer.
    """
    vacuous = [c.id for c in _ALL_CASES
               if not c.facts and not c.behaviors and not c.forbidden]
    assert not vacuous, "这些 case 没有任何断言，必然空过：" + ", ".join(vacuous)


def test_forbidden_strings_belong_to_a_different_entity():
    """A forbidden string is only valid if no *correct* answer could contain it.

    h07 ("我持仓里每只的机构持股比例") forbade "Vanguard 8.94%" — which is NVDA's
    real institutional holding. Any correct answer reporting NVDA necessarily
    contains it, so the case was unpassable by construction, and two rounds of
    prompt work were spent blaming the model for it.

    The rule that would have caught it: a forbidden string needs a *specific*
    subject the case is about, so that the string can be shown to belong to
    someone else. A book-wide question has no such subject, and must express its
    requirement as a behaviour ("did it admit the gap") instead.
    """
    offenders = []
    for case in _ALL_CASES:
        if not case.forbidden:
            continue
        entity = case.ticker or case.extra.get("etf") or case.extra.get("manager")
        if not entity:
            offenders.append(f"{case.id}（无特定主体，却用禁止字符串表达要求）")
    assert not offenders, "禁止字符串用错了地方：" + ", ".join(offenders)


def test_every_required_tool_exists():
    unknown = []
    for case in _ALL_CASES:
        for tool in case.must_call + case.wasteful_tools + case.must_not_call:
            if tool not in SPECS_BY_NAME:
                unknown.append(f"{case.id}: {tool}")
    assert not unknown, "引用了不存在的工具：" + ", ".join(unknown)


def test_case_ids_are_unique_and_categories_known():
    ids = [c.id for c in _ALL_CASES]
    assert len(ids) == len(set(ids))
    assert {c.category for c in _ALL_CASES} <= set(CATEGORIES)
    assert {c.expected_path for c in _ALL_CASES} <= {"single_hop", "agent", "slash"}


def test_classifier_extras_reference_real_cases_and_fields():
    known_fields = {"manager", "etf", "release_type", "period", "days_horizon",
                    "days_back", "target_price", "direction"}
    holdout_ids = {c.id for c in HOLDOUT}
    for extras, ids in ((CLASSIFIER_EXTRAS, set(_CASE_BY_ID)), (HOLDOUT_EXTRAS, holdout_ids)):
        for case_id, extra in extras.items():
            assert case_id in ids, case_id
            assert set(extra) <= known_fields, f"{case_id}: {set(extra) - known_fields}"


def test_the_holdout_set_repeats_no_development_query():
    """A held-out case that is a development case under a new id measures
    nothing. Compared with spaces and terminal punctuation removed, because
    「NVDA为什么涨？」 is the user's spelling of s01 and *that* is what the
    holdout is for — but 「我持仓里哪只最危险」 verbatim is r01."""
    def key(q: str) -> str:
        return q.replace(" ", "").rstrip("？?。!！")
    dev = {key(c.query): c.id for c in CASES}
    repeats = [f"{c.id} = {dev[key(c.query)]}" for c in HOLDOUT if key(c.query) in dev]
    # x01 is the one deliberate near-duplicate: s01 as actually typed.
    assert repeats == ["x01 = s01"], repeats
    assert all(c.id.startswith("x") for c in HOLDOUT), "留出集 id 以 x 开头，一眼能认出来"


def test_registry_can_serve_every_single_hop_case():
    """The fast path must actually reach a tool for each case labelled single_hop."""
    registry = build_eval_registry()
    broken = []
    for case in _ALL_CASES:
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
