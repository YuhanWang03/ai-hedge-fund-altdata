"""Tests for the agent loop.

An agent is non-deterministic where the bot is deterministic, so the bot's
testing strategy (assert the formatted card is byte-equal) does not carry over.
What *is* testable is the loop's control flow, and that is what these tests pin
down: does a tool failure become an observation, does a repeated call get
suppressed, does the budget always end in an answer, does an invented figure
trigger a repair. The model is scripted so each of those is an assertion rather
than an anecdote.

Runs under pytest, and also standalone (``python v2/agent/test_agent.py``) so it
works in a sandbox with no third-party packages installed.
"""

from __future__ import annotations

import pathlib
import sys

# Running this file directly (IDE green arrow) puts v2/agent/ on sys.path, not the
# repo root, so `import v2.agent` would fail. Under pytest this is a no-op.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent import grounding
from v2.agent.baseline import INTENT_TO_TOOL, ScriptedClassifier, run_baseline
from v2.agent.context import clip
from v2.agent.fixtures import PORTFOLIO_FIXTURES, build_registry
from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall, _parse_arguments
from v2.agent.loop import AgentConfig, run_agent
from v2.agent.registry import (
    SPECS_BY_NAME,
    TOOL_SPECS,
    FixtureExecutor,
    ToolRegistry,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _call(name: str, index: int = 0, **arguments) -> ToolCall:
    import json
    return ToolCall(id=f"call_{index}", name=name, arguments=arguments,
                    raw_arguments=json.dumps(arguments))


def _acts(*calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls),
                       prompt_tokens=100, completion_tokens=20)


def _says(text: str) -> LLMResponse:
    return LLMResponse(text=text, prompt_tokens=100, completion_tokens=40,
                       finish_reason="stop")


def _registry(**kwargs) -> ToolRegistry:
    return build_registry(**kwargs)


# ---------------------------------------------------------------------------
# registry — the tool surface and its policy gate
# ---------------------------------------------------------------------------

def test_specs_are_well_formed():
    names = [s.name for s in TOOL_SPECS]
    assert len(names) == len(set(names)), "tool names must be unique"
    assert len(TOOL_SPECS) == 24, "one tool per bot intent"
    for spec in TOOL_SPECS:
        assert spec.parameters.get("type") == "object"
        assert "." in spec.target, "target must be a dotted path"
        schema = spec.to_openai_schema()
        assert schema["function"]["name"] == spec.name
        assert schema["function"]["description"]
        for required in spec.parameters.get("required", []):
            assert required in spec.parameters["properties"]
        if spec.invoke_style == "args":
            for key in spec.arg_order:
                assert key in spec.parameters["properties"]


def test_every_target_resolves_in_the_real_source():
    """The 24 dotted paths must exist in v2/bot — checked by parsing, not importing.

    Importing the responders needs numpy, telegram and the rest of the stack, so
    a sandbox could never catch a typo'd target. Parsing the source can, and it
    is the check that matters: registry drift against a renamed responder would
    otherwise only surface at runtime, one tool call at a time.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    cache: dict[str, set[str]] = {}
    for spec in TOOL_SPECS:
        module_path, _, attribute = spec.target.rpartition(".")
        if module_path not in cache:
            source = root / (module_path.replace(".", "/") + ".py")
            assert source.exists(), f"{spec.target}: no such module {source}"
            tree = ast.parse(source.read_text(encoding="utf-8"))
            cache[module_path] = {
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
        assert attribute in cache[module_path], f"{spec.target} not found in {module_path}"


def test_source_reads_always_declare_utf8():
    """No file read in this package may rely on the platform's default encoding.

    Python picks the locale codec when `encoding` is omitted, which is GBK on a
    Chinese Windows install. Reading v2/bot/responders.py — full of curly quotes
    and emoji — then dies with UnicodeDecodeError on a contributor's machine and
    passes on CI. Declaring utf-8 is the only portable option, so this asserts it
    rather than trusting review to catch the next one.
    """
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parent
    offenders: list[str] = []
    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("read_text", "write_text", "open"):
                continue
            if name == "open" and isinstance(func, ast.Attribute):
                pass  # Path.open — same rule
            if not any(kw.arg == "encoding" for kw in node.keywords):
                offenders.append(f"{module.name}:{node.lineno} {name}()")

    assert not offenders, "file access without explicit encoding: " + ", ".join(offenders)


def test_unknown_tool_returns_observation_not_exception():
    result = _registry().call("portfolio_viewz", {})
    assert not result.ok
    assert result.error_kind == "unknown_tool"
    assert "portfolio_view" in result.content, "should suggest the near miss"


def test_missing_required_argument_is_reported_to_the_model():
    result = _registry().call("earnings_view", {})
    assert not result.ok and result.error_kind == "bad_arguments"
    assert "ticker" in result.content


def test_arguments_are_coerced_and_hallucinated_ones_dropped():
    seen: dict = {}

    def executor(spec, args):
        seen.update(args)
        return "ok"

    registry = ToolRegistry(executor=executor, allow_mutations=True)
    registry.call("alert_set", {"ticker": "nvda", "target_price": "130.5",
                                "direction": "ABOVE", "urgency": "high"})
    assert seen == {"ticker": "NVDA", "target_price": 130.5, "direction": "above"}, seen


def test_enum_violation_is_rejected():
    result = _registry().call("pnl_period", {"period": "quarter"})
    assert not result.ok and result.error_kind == "bad_arguments"


def test_mutations_are_blocked_by_default_and_openable():
    blocked = _registry().call("watchlist_add", {"ticker": "ARM"})
    assert not blocked.ok and blocked.error_kind == "mutation_blocked"

    # The refusal has to say *why*, or the model guesses — and it guessed
    # wrong live: 「本次运行中写入操作被禁用…请在写入功能恢复后重新执行」,
    # inventing an outage that will never end. Read-only is a property of this
    # path, and the useful thing to hand back is the command that does work.
    assert "BY DESIGN" in blocked.content
    assert "not an outage" in blocked.content
    assert "加入关注列表" in blocked.content, "要给出用户照抄就能执行的那句话"

    allowed = ToolRegistry(executor=lambda spec, args: "added",
                           allow_mutations=True).call("watchlist_add", {"ticker": "ARM"})
    assert allowed.ok


def test_tool_exception_becomes_a_failed_observation():
    result = _registry().call("eight_k_view", {"ticker": "SMCI"})
    assert not result.ok
    assert result.error_kind == "SimulatedToolFailure"
    assert result.as_observation().startswith("[TOOL_ERROR")


def test_long_observations_are_truncated_at_the_boundary():
    registry = ToolRegistry(executor=lambda spec, args: "x" * 9000,
                            max_observation_chars=500)
    result = registry.call("portfolio_view", {})
    assert result.ok and len(result.content) < 600
    assert result.meta["truncated"] is True


# ---------------------------------------------------------------------------
# loop — control flow
# ---------------------------------------------------------------------------

def test_multi_step_run_uses_earlier_results_to_choose_later_calls():
    """The defining behaviour: step 2's calls depend on step 1's observation."""
    llm = ScriptedLLM([
        _acts(_call("portfolio_view"), text="先看持仓"),
        _acts(_call("earnings_view", 1, ticker="CRWD"),
              _call("earnings_view", 2, ticker="SMCI"),
              _call("insider_view", 3, ticker="CRWD"),
              text="对权重最高的两只查财报"),
        _says("CRWD 最危险：占仓 22.4%，2026-09-06 财报，90 天内 3 次内部人卖出。"),
    ])
    result = run_agent("我持仓里哪只最危险", llm=llm, registry=_registry())

    assert result.stop_reason == "final_answer"
    assert result.trajectory.llm_calls == 3
    assert result.trajectory.tool_calls == 4
    assert result.trajectory.failed_tool_calls == 0
    assert set(result.trajectory.distinct_tools()) == {
        "portfolio_view", "earnings_view", "insider_view"}
    assert "CRWD" in result.answer


def test_parallel_calls_land_in_one_step():
    llm = ScriptedLLM([
        _acts(_call("earnings_view", 1, ticker="CRWD"),
              _call("earnings_view", 2, ticker="SMCI"),
              _call("earnings_view", 3, ticker="NVDA")),
        _says("三家财报日期见上。CRWD 2026-09-06，SMCI 2026-09-09，NVDA 2026-11-19。"),
    ])
    result = run_agent("我的持仓什么时候发财报", llm=llm, registry=_registry())
    assert len(result.trajectory.steps[0].results) == 3
    assert result.trajectory.llm_calls == 2


def test_failed_tool_does_not_end_the_run():
    """The bot surfaces the exception class to the user; the loop routes around it."""
    llm = ScriptedLLM([
        _acts(_call("eight_k_view", 1, ticker="SMCI")),      # fixture raises
        _acts(_call("explain_move", 2, ticker="SMCI")),      # model picks another route
        _says("SMCI 今日 -5.40%，8-K 数据源超时，改用异动归因：渠道调研指出订单递延。"),
    ])
    result = run_agent("SMCI 最近有什么事", llm=llm, registry=_registry())

    assert result.stop_reason == "final_answer"
    assert result.trajectory.failed_tool_calls == 1
    assert result.trajectory.tool_calls == 2
    observations = [r.error_kind for s in result.trajectory.steps for r in s.results]
    assert "SimulatedToolFailure" in observations


def test_repeated_identical_call_is_suppressed():
    llm = ScriptedLLM([
        _acts(_call("portfolio_view")),
        _acts(_call("portfolio_view")),          # same signature — the classic loop
        _says("持仓见上，总市值 $184,320.55。"),
    ])
    result = run_agent("我的持仓", llm=llm, registry=_registry())

    assert result.deduped_calls == 1
    duplicate = result.trajectory.steps[1].results[0]
    assert duplicate.error_kind == "duplicate_call"


def test_malformed_tool_arguments_are_fed_back_not_raised():
    bad = ToolCall(id="c1", name="earnings_view", arguments={},
                   raw_arguments="{ticker: CRWD", parse_error="invalid JSON arguments")
    llm = ScriptedLLM([
        _acts(bad),
        _acts(_call("earnings_view", 2, ticker="CRWD")),
        _says("CRWD 下次财报 2026-09-06。"),
    ])
    result = run_agent("CRWD 财报", llm=llm, registry=_registry())
    assert result.trajectory.steps[0].results[0].error_kind == "bad_json"
    assert result.stop_reason == "final_answer"


def test_budget_exhaustion_still_produces_an_answer():
    """A truncated run must not return nothing — the last turn is forced."""
    llm = ScriptedLLM([
        _acts(_call("portfolio_view")),
        _acts(_call("risk_view")),
        _says("在预算内能给的结论：组合前 3 大持仓合计 54.7%，集中度超标。"),
    ])
    result = run_agent("分析我的组合", llm=llm, registry=_registry(),
                       config=AgentConfig(max_steps=3))
    assert result.answer
    assert result.forced_final is True
    assert result.trajectory.llm_calls <= 3


def test_tool_call_budget_forces_the_final_turn():
    llm = ScriptedLLM([
        _acts(_call("earnings_view", 1, ticker="CRWD"),
              _call("earnings_view", 2, ticker="SMCI")),
        _says("两家财报日期见上。"),
    ])
    result = run_agent("财报", llm=llm, registry=_registry(),
                       config=AgentConfig(max_tool_calls=2, max_steps=5))
    assert result.trajectory.tool_calls == 2
    assert result.answer


# ---------------------------------------------------------------------------
# grounding — the guarantee that survives letting the model write
# ---------------------------------------------------------------------------

def test_grounding_flags_invented_figures():
    report = grounding.check("CRWD 占仓 22.4%，回撤 -37.9%。", PORTFOLIO_FIXTURES["portfolio_view"])
    assert "37.9" in "".join(report.ungrounded)
    assert not report.ok


def test_grounding_exempts_small_integers_and_years():
    report = grounding.check("前 3 大持仓，2026 年至今。", "irrelevant corpus")
    assert report.total == 0 and report.exempt == 2 and report.ok


def test_grounding_accepts_a_rounded_value():
    """15,851.57 written as 15,852 is the card's number, not a new one."""
    assert grounding.check("SMCI 市值 15,852 美元", "· SMCI $15,851.57 · 8.6%").ok


def test_grounding_accepts_a_unit_conversion():
    """$57.80B and 578 亿 are the same quantity; only the scale word differs."""
    assert grounding.check("AAPL 持仓 578 亿", "AAPL $57.80B（22.0%）").ok
    assert grounding.check("COO 卖出 919 万", "卖出 41,000 股（$9.19M）").ok


def test_grounding_exempts_identifiers():
    """8-K Item 5.02 names a section — demanding it trace to data is incoherent."""
    report = grounding.check("披露了 Item 5.02 与 Item 1.01", "无关观测")
    assert report.ok and report.exempt == 2


def test_grounding_still_rejects_unshown_arithmetic_and_invention():
    observations = "CRWD 22.4% · NVDA 18.2% · MSFT 14.1%"
    assert not grounding.check("前三合计 54.7%", observations).ok, "不写算式的和仍要拒"
    assert not grounding.check("年化波动率 63.8%", observations).ok


def test_shown_arithmetic_over_traceable_inputs_is_accepted():
    """Three sweeps showed the model would not comply with 'show your work', and
    accepting bare sums would gut the guarantee. Requiring the derivation to be
    *visible* keeps both properties."""
    observations = "CRWD 22.4% · NVDA 18.2% · MSFT 14.1%"
    report = grounding.check("前三合计 22.4% + 18.2% + 14.1% = 54.7%", observations)
    assert report.ok and report.derived == 1


def test_a_shown_derivation_that_does_not_add_up_is_still_rejected():
    observations = "CRWD 22.4% · NVDA 18.2%"
    assert not grounding.check("22.4% 与 18.2% 相加得 99.9%", observations).ok


def test_a_derivation_over_invented_inputs_is_rejected():
    """Faking this needs addends that each trace *and* sum to the target."""
    observations = "CRWD 22.4%"
    assert not grounding.check("31.5% + 32.3% = 63.8%", observations).ok


def test_grounding_matches_across_comma_formatting():
    report = grounding.check("总市值 184320.55 美元。", "总市值 $184,320.55")
    assert report.ok and report.grounded == 1


def test_ungrounded_answer_triggers_one_repair_round():
    llm = ScriptedLLM([
        _acts(_call("portfolio_view")),
        _says("CRWD 占仓 22.4%，年化波动率 63.8%。"),   # 63.8 is invented
        _says("CRWD 占仓 22.4%，是第一大持仓。"),        # repaired
    ])
    result = run_agent("最大持仓是谁", llm=llm, registry=_registry())

    assert result.repairs == 1
    assert result.stop_reason == "final_answer"
    assert result.grounding.ok
    assert "63.8" not in result.answer


def test_repair_happens_at_most_once():
    llm = ScriptedLLM([
        _acts(_call("portfolio_view")),
        _says("回撤 -37.9%。"),
        _says("回撤 -41.2%。"),   # still invented; loop must not spin
    ])
    result = run_agent("回撤多少", llm=llm, registry=_registry())
    assert result.repairs == 1
    assert result.stop_reason == "final_answer_ungrounded"


# ---------------------------------------------------------------------------
# attribution — the number is real, the subject is not
# ---------------------------------------------------------------------------

def _records(*rows):
    return [(tool, args, content, True) for tool, args, content in rows]


def test_attribution_catches_another_entitys_figure():
    """h07: only NVDA had institutional data; the model gave AMD NVDA's numbers."""
    from v2.agent import attribution

    report = attribution.check(
        "NVDA Vanguard 8.94%；AMD Vanguard 8.94%。",
        _records(("holders", {"ticker": "NVDA"}, "Vanguard 8.94% BlackRock 7.31%"),
                 ("holders", {"ticker": "AMD"}, "fixture 未记录该 ticker 的机构持仓。")))
    assert not report.ok
    assert ("AMD", "8.94", ("NVDA",)) in report.misattributed


def test_attribution_catches_a_false_frame():
    """h04: every figure is correctly attributed to its stock — but ARKQ has no
    card, so presenting ARKK's holdings under ARKQ's name is still false."""
    from v2.agent import attribution

    report = attribution.check(
        "ARKQ 前三大持仓：TSLA 9.80%、COIN 7.20%。",
        _records(("etf_view", {"symbol": "ARKQ"}, "fixture 模式未记录该 ETF。"),
                 ("etf_view", {"symbol": "ARKK"}, "前三：TSLA 9.80% · COIN 7.20%")))
    assert not report.ok and report.empty_presented


def test_attribution_accepts_an_acknowledged_gap():
    from v2.agent import attribution

    report = attribution.check(
        "ARKQ 没有记录数据。ARKK 前三：TSLA 9.80%。",
        _records(("etf_view", {"symbol": "ARKQ"}, "fixture 模式未记录该 ETF。"),
                 ("etf_view", {"symbol": "ARKK"}, "前三：TSLA 9.80% · COIN 7.20%")))
    assert report.ok


def test_attribution_does_not_flag_a_correct_multi_ticker_answer():
    """The false positive that matters: a figure belongs to the nearest entity
    named before it, so a list of positions must not cross-contaminate."""
    from v2.agent import attribution

    assert attribution.check(
        "NVDA 占仓 18.2%，CRWD 占仓 22.4%。",
        _records(("portfolio_view", {}, "CRWD 22.4% NVDA 18.2%"))).ok
    assert attribution.check(
        "CRWD beat 6.1%，SMCI miss 23.6%。",
        _records(("earnings_view", {"ticker": "CRWD"}, "EPS beat +6.1%"),
                 ("earnings_view", {"ticker": "SMCI"}, "EPS miss -23.6%"))).ok


def test_a_figure_can_belong_to_the_entity_that_follows_it():
    """Chinese puts the modifier before its head, so reading left to right
    misreads 「但被占仓 66.3% 的 IVV 微跌」 as NVDA's 66.3 — seen live, on a
    correct answer. Both directions matter: the figure is *reattributed*, not
    excused, so a genuinely wrong one is still caught."""
    from v2.agent import attribution

    assert attribution.check(
        "靠 MU 大涨 +4.72% 和 NVDA 微涨拉动，但被占仓 66.3% 的 IVV 微跌 -0.47% 抵消。",
        _records(("risk_view", {}, "BROAD 大盘 ETF 66.3% · Top1 IVV 66.3%"),
                 ("explain_move", {"ticker": "IVV"}, "IVV -0.47%"),
                 ("explain_move", {"ticker": "MU"}, "MU +4.72%"))).ok

    # The same construction with the wrong ticker attached is a real finding,
    # and no forward window covers it — the figure precedes the only mention.
    report = attribution.check(
        "被浮亏 -35.9% 的 NVDA 拖累。",
        _records(("portfolio_view", {"ticker": "ARM"}, "ARM -35.9%")))
    assert report.misattributed == [("NVDA", "-35.9", ("ARM",))]

    # A comma between the figure and the next ticker is not that construction.
    assert not attribution.check(
        "NVDA 占仓 18.2%，CRWD 占仓 22.4%。",
        _records(("portfolio_view", {}, "CRWD 22.4% NVDA 18.2%"))).misattributed


def test_a_clause_naming_several_entities_cannot_be_paired_by_proximity():
    """The shape behind twelve of the fourteen false positives in one sweep.

    「22.4%（CRWD）+ 18.2%（NVDA）+ 14.1%（MSFT）= 54.7%」 and
    「CRWD + NVDA + MSFT 合计 22.4% + 18.2% + 14.1%」 pair figure to name
    *structurally*; "nearest name before" reads every pair off by one and
    reports the whole sum as misattributed.

    So the rule became: proximity carries information only while a clause names
    one entity. Where several share it, the check abstains — but only if one of
    them owns the figure. When none does, the finding stands, which is what
    keeps h07 detectable.
    """
    from v2.agent import attribution

    card = "CRWD 22.4% · NVDA 18.2% · MSFT 14.1% · 前三合计 54.7%"
    for answer in (
        "- 前三大持仓 CRWD + NVDA + MSFT 合计 22.4% + 18.2% + 14.1% = 54.7%。",
        "前三大为 CRWD（22.4%）、NVDA（18.2%）、MSFT（14.1%），合计 54.7%。",
        "前 3 大合计 22.4%（CRWD）+ 18.2%（NVDA）+ 14.1%（MSFT）= 54.7%。",
    ):
        assert attribution.check(answer, _records(("risk_view", {}, card))).ok, answer

    # Several names, none of which owns it — still a finding.
    assert not attribution.check(
        "CRWD 和 NVDA 的机构比例都是 8.94%。",
        _records(("holders", {"ticker": "MU"}, "MU Vanguard 8.94%"))).ok


def test_a_bare_comparison_operator_counts_too():
    """「（-5.58pp 对 -3.14pp）」 — 对 is the operator. A figure has to sit in
    front of it, or the far commoner uses (对…来说, 面对) would swallow whatever
    follows them."""
    from v2.agent import attribution

    assert attribution.check(
        "PLTR 跑输大盘的幅度更大（-5.58pp 对 -3.14pp）。",
        _records(("explain_move", {"ticker": "TSLA"}, "相对强度 -3.14pp"),
                 ("explain_move", {"ticker": "PLTR"}, "相对强度 -5.58pp"))).ok

    assert not attribution.check(
        "对 NVDA 来说 18.2% 偏高。",
        _records(("portfolio_view", {"ticker": "ARM"}, "ARM 18.2%"))).ok


def test_a_clause_that_disclaims_the_data_is_not_claiming_it():
    """h04's own correct answer: 「ARKQ 的工具返回里没有任何数据，那些数字
    （TSLA 9.80%、PATH 22.4%）都不是 ARKQ 的」. Flagging that punishes exactly
    the write-up the case asks for; the empty-entity pass has trusted this same
    phrase list from the start."""
    from v2.agent import attribution

    assert attribution.check(
        "ARKQ 的工具返回里没有任何数据，那些数字（TSLA 9.80%、PATH 22.4%）都不是 ARKQ 的。",
        _records(("etf_view", {"symbol": "ARKQ"}, "ARKQ 未记录"),
                 ("etf_view", {"symbol": "ARKK"}, "TSLA 9.80%"),
                 ("portfolio_view", {}, "CRWD 22.4%"))).ok


def test_a_comparison_operand_belongs_to_the_other_side():
    """「NVDA 的 EPS 更高（$1.31 vs $0.71），超预期也更大（+5.6% vs +2.9%）」 —
    every second number is AMD's, and nothing in that clause says so."""
    from v2.agent import attribution

    assert attribution.check(
        "NVDA 的 EPS 绝对值更高（$1.31 vs $0.71），超预期幅度也更大（+5.6% vs +2.9%）。",
        _records(("earnings_view", {"ticker": "NVDA"}, "EPS $1.31 · beat +5.6%"),
                 ("earnings_view", {"ticker": "AMD"}, "EPS $0.71 · beat +2.9%"))).ok


def test_a_level_is_not_a_measurement():
    """「VIX 18.40…仍处于 20 以下的舒适区」 — 20 is a line being compared against,
    and it happened to be CRWD's concentration threshold in the risk card."""
    from v2.agent import attribution

    assert attribution.check(
        "VIX 18.40，仍处于 20 以下的相对舒适区。",
        _records(("macro_view", {}, "VIX 18.40"),
                 ("risk_view", {}, "最大单一持仓 CRWD 22.4%（阈值 20%）"))).ok


def test_a_card_row_does_not_own_the_next_rows_figures():
    """The observation side was deliberately left unbounded, on the reasoning
    that extra owners can only *reduce* false positives. The reasoning was
    wrong: a ticker at the end of one card line went on owning the next line's
    portfolio-level figures, so 「组合当前回撤 -4.20%」 became MSFT's."""
    from v2.agent import attribution

    card = ("<b>行业暴露</b>\n· 软件/安全 36.5%（CRWD + MSFT）\n"
            "<b>回撤</b>\n· 组合当前回撤 -4.20%（峰值 2026-08-14）")
    report = attribution.check("SMCI 深亏，而组合整体回撤为 -4.20%。",
                               _records(("risk_view", {}, card)))
    assert report.ok, report.summary()


def test_a_finding_carries_the_line_it_came_from():
    """「MSFT←18.2(实为 NVDA)」 says what the check concluded and nothing about
    why. Five plausible reconstructions of that sentence failed to reproduce it,
    which left the next fix a guess — the same unactionable verdict this package
    rejects for grounding ("数字无法溯源" without naming the figure)."""
    from v2.agent import attribution

    report = attribution.check(
        "组合概览：\nNVDA 权重 18.2%。\nMSFT 浮亏 -35.9%，仓位偏小。",
        _records(("portfolio_view", {"ticker": "ARM"}, "ARM -35.9%"),
                 ("portfolio_view", {"ticker": "NVDA"}, "NVDA 18.2%")))
    assert report.misattributed and len(report.evidence) == len(report.misattributed)
    assert "MSFT 浮亏 -35.9%" in report.evidence[0]
    assert "NVDA 权重" not in report.evidence[0], "证据要是出问题的那一行"


def test_a_benchmark_is_not_the_subject():
    """「同期 SPY +0.40% → 相对强度 -3.14pp」 is a sentence about TSLA. SPY is
    what TSLA is measured against, so the delivery figure two clauses later is
    TSLA's — not SPY's.

    This shape produced the first false positive of the series (「相对 SMH 逆势
    -8.30pp」) and, twelve fixes later, the last two on the evaluation set. The
    rule matches the *construction* rather than listing SPY / SMH / XLK: the
    benchmark is whatever card was fetched, and one of them (IVV) is a position
    this user actually holds, so a stoplist would miss cases and break a real
    one.
    """
    from v2.agent import attribution

    card = ("📈 <b>TSLA 为什么动</b>\n· 同期 SPY +0.40% → 相对强度 -3.14pp ★ 逆势\n"
            "· Tier-1 归因：Reuters「欧洲 8 月交付量同比 -14%」")
    assert attribution.check(
        "TSLA 今日下跌，同期 SPY +0.40%，相对强度 -3.14pp；欧洲交付量同比 -14% 是主因。",
        _records(("explain_move", {"ticker": "TSLA"}, card))).ok

    # The same ticker as a *subject* is checked as usual.
    assert attribution.check(
        "SPY 交付量同比 -14%。",
        _records(("explain_move", {"ticker": "TSLA"}, card))
    ).misattributed == [("SPY", "-14", ("TSLA",))]


def test_a_date_is_not_a_negative_number():
    """A hyphen in front of a day of the month is a minus sign to any number
    extractor. The earnings calendar is a column of them, one ticker per row, so
    a list where every single entry was correct came back as four
    misattributions: 「LRCX←-21(实为 MU)」, 「INTC←-22(实为 LRCX)」…
    """
    from v2.agent import attribution

    # The card lists the date first and the ticker after it, so each day lands in
    # the *previous* row's window — which is what shifted every entry by one.
    answer = "· MU：09-30\n· LRCX：10-21\n· INTC：10-22\n· UNH：10-27"
    card = ("09-30 (D-26) MU\n10-21 (D-47) LRCX\n"
            "10-22 (D-48) INTC\n10-27 (D-53) UNH")
    assert attribution.check(answer, _records(("earnings_calendar", {}, card))).ok

    # …and a real quantity that merely looks adjacent is still checked.
    assert not attribution.check(
        "LRCX 浮亏 -21.4%。",
        _records(("portfolio_view", {"ticker": "MU"}, "MU -21.4%"))).ok


def test_the_summary_says_when_it_stopped_listing():
    """「4 处张冠李戴：A, B, C」 reads as a complete list of three."""
    from v2.agent.attribution import AttributionReport

    report = AttributionReport(
        misattributed=[(f"T{i}", "1.5", ("X",)) for i in range(4)])
    assert report.summary().endswith("…")


def test_its_own_figure_written_shorter_is_still_its_own():
    """「QCOM …虽然浮亏 30%」 is QCOM's own -30.39% rounded to whole percent.
    The literal "30" also sits in the risk card's threshold («单票 IVV > 30%»),
    so ownership by exact string called it IVV's and flagged a correct line.

    Tolerance is half of the last written digit — what "rounded to this
    precision" means — so it cannot swallow a figure that is merely nearby."""
    from v2.agent import attribution

    assert attribution.check(
        "QCOM：近 20 日上涨 +5.1%，虽然浮亏 30%，但资金在流入。",
        _records(("risk_view", {}, "单票 IVV > 30% / BROAD 行业 > 30%"),
                 ("portfolio_view", {}, "QCOM -30.39%"))).ok

    # Nothing of QCOM's rounds to 30, so borrowing ARM's is still caught.
    assert attribution.check(
        "QCOM 浮亏 30%。",
        _records(("insider_view", {"ticker": "ARM"}, "ARM 合计 30"))
    ).misattributed == [("QCOM", "30", ("ARM",))]


def test_the_account_mode_label_is_not_a_holding():
    """「📝 PAPER」 is the account mode the card prints, not a position. It owned
    the portfolio total, which then read as misattributed to ARM."""
    from v2.agent import attribution

    assert attribution.check(
        "ARM 市值仅 $1,260，占组合约 1.2%（$1,260 / $100,750）。",
        _records(("portfolio_view", {},
                  "📝 PAPER · 组合价值 $100,750 · ARM $1,260"))).ok


def test_a_window_size_and_a_shown_derivation_are_not_misattributions():
    """Two live false positives, from the same run.

    「接近 52 周高点」 — 52 is the size of a window, not a quantity belonging to
    anyone, and it was reported against the neighbouring name.

    「EPS $2.46 vs $1.85（+33.0%）」 — the model computed NVDA's surprise, so no
    card owns 33; it appeared verbatim in AMD's card and was called AMD's.
    Grounding refuses ratios because accepting one there lets a fabrication
    through; here a false positive rejects a correct answer, so the same
    evidence gets the opposite rule.
    """
    from v2.agent import attribution

    # 52 is owned only by INTC's card, and MU is the name in front of it.
    assert attribution.check(
        "MU 接近 52 周高点、高于 200 日均线。",
        _records(("summary", {"ticker": "INTC"}, "INTC 52 周区间 · 200 日均线"))).ok

    # 33.0 is owned only by AMD's card; NVDA's line computes it in the open.
    assert attribution.check(
        "NVDA：营收 $96.22B vs 预期 $83.67B（+15.0%），EPS $2.46 vs $1.85（+33.0%）",
        _records(("earnings_view", {"ticker": "AMD"}, "AMD 净利率 33.0% · 15.0%"))).ok

    # A figure with no arithmetic behind it is still checked.
    assert not attribution.check(
        "NVDA 浮亏 -35.9%。",
        _records(("portfolio_view", {"ticker": "ARM"}, "ARM -35.9%"))).ok


def test_ordinals_and_counts_are_not_attribution_findings():
    """The check's own feedback loop, seen live: it complained that IVV was
    given a "1", the repair round wrote 「risk_view 里没有 IVV 的「1」这个数据」,
    and that sentence puts a 1 right after IVV — so it complained again about
    the apology it had caused. Grounding exempted these from the start."""
    from v2.agent import attribution

    assert attribution.check(
        'risk_view 里没有 IVV 的"1"这个数据。Top 1 是 IVV。',
        _records(("portfolio_view", {"ticker": "ARM"}, "ARM 1 笔"),
                 ("risk_view", {"ticker": "XLV"}, "XLV 1 只"))).ok
    # …but a real figure is still caught, and reported once, not per repetition.
    report = attribution.check(
        "IVV 浮亏 -35.71%，IVV 浮亏 -35.71%。",
        _records(("portfolio_view", {"ticker": "ARM"}, "ARM -35.71%")))
    assert len(report.misattributed) == 1


def test_the_answer_drops_the_models_narration_of_its_own_process():
    from v2.agent import presentation

    assert presentation.strip_deliberation(
        "你说得对，我犯了张冠李戴的错误。让我重新核对。\n\n## 结论：ARM\n\nARM -35.71%。"
    ) == "## 结论：ARM\n\nARM -35.71%。"
    assert presentation.strip_deliberation(
        '用户问"为什么"，没有上下文。让我直接询问澄清。\n\n---\n\n请补充你想问的对象。'
    ) == "请补充你想问的对象。"
    # A real answer that merely uses headings keeps every word.
    intact = "CRWD 占仓 22.4%，是第一大持仓。\n\n## 依据\n\n集中度 54.7%。"
    assert presentation.strip_deliberation(intact) == intact
    # An unmarked opener is dropped only when a real answer follows it — this is
    # the narrower second rule, added after the apology reached users twice.
    assert presentation.strip_deliberation(
        "你说得对，我犯了把 ARKK 的数据安到 ARKQ 头上的错误。"
        "ARKQ 的工具返回里没有任何数据，那些数字都不是 ARKQ 的，下面逐条说明来源。"
    ).startswith("ARKQ 的工具返回里")
    # …and never when what follows is too short to be one: dropping the opener
    # there would more likely be discarding the answer itself.
    short = "让我看看持仓。CRWD 22.4%。"
    assert presentation.strip_deliberation(short) == short
    # 让我们 is ordinary phrasing, not narration.
    plural = "让我们看看这几只半导体。NVDA +17.8%，MU +5.1%，ARM -35.9%，分化明显。"
    assert presentation.strip_deliberation(plural) == plural
    # A long marked preamble is still never discarded wholesale.
    long_one = "让我重新核对。" + "数" * presentation.MAX_PREAMBLE + "\n\n## 结论\n\nARM。"
    assert "数" * 100 in presentation.strip_deliberation(long_one)


def test_markdown_becomes_the_html_telegram_renders():
    """The bot sends parse_mode=HTML because every responder card is HTML; the
    model writes Markdown. Live, that meant answers arrived with their markup
    showing — "# 结论：", "**小幅上涨**", pipe tables drawn by hand."""
    from v2.agent import presentation as pres

    assert pres.to_telegram_html("# 结论：整体**小幅上涨**") == "<b>结论：整体小幅上涨</b>"
    assert pres.to_telegram_html("- 今日 +0.28%") == "· 今日 +0.28%"
    assert pres.to_telegram_html("看 `pnl_view`") == "看 <code>pnl_view</code>"
    # Telegram has no table tag, so columns only survive inside <pre>, padded
    # by display width — CJK glyphs take two cells.
    table = pres.to_telegram_html("| 个股 | P/L |\n|---|---|\n| ARM | -35.4% |")
    assert table.startswith("<pre>") and "个股  P/L" in table
    assert "ARM   -35.4%" in table
    # A stray angle bracket must be escaped, not shipped as a broken tag.
    assert pres.to_telegram_html("a < b & c") == "a &lt; b &amp; c"
    # Arithmetic is not italics.
    assert pres.to_telegram_html("3*4 与 5*6") == "3*4 与 5*6"


def test_a_table_too_wide_to_align_wraps_instead_of_scrolling():
    """<pre> preserves columns and preserves them off the side of the screen.
    Live, a two-column table whose second cell held nine tickers came out 140
    characters wide — text that wraps beats a grid you have to drag."""
    from v2.agent import presentation as pres

    wide = ("| 状态 | 标的 | 累计 P/L |\n|---|---|---|\n"
            "| 🟢 盈利 | IVV +2.39%、NVDA +17.78%、MU +5.09% | — |")
    rendered = pres.to_telegram_html(wide)
    assert "<pre>" not in rendered, "太宽就不该再用等宽块"
    assert "🟢 盈利" in rendered and "NVDA +17.78%" in rendered
    assert "—" not in rendered, "空单元格不该被读出来"

    narrow = pres.to_telegram_html("| 指标 | 数值 |\n|---|---|\n| 今日 | +0.21% |")
    assert narrow.startswith("<pre>") and "指标  数值" in narrow
    assert all(pres._display_width(line) <= pres.MAX_PRE_WIDTH
               for line in pres.to_plain_text(narrow).split("\n"))


def test_the_plain_text_fallback_removes_markup_rather_than_showing_it():
    from v2.agent import presentation as pres

    assert pres.to_plain_text("<b>结论</b>：a &lt; b") == "结论：a < b"


def test_attribution_window_stops_at_a_line_or_bullet():
    """The demo's own false positive: a benchmark named at the end of one
    bullet is not the subject of the next bullet's numbers.

    SMH is the last entity mentioned on its line, so with the window bounded
    only by the next entity mention it reached into the following bullet and
    reported SMCI's earnings history as SMH's."""
    from v2.agent import attribution

    answer = ("<b>SMCI</b>\n"
              "· 今日 -5.40%，相对 SMH 逆势 -8.30pp\n"
              "· 上次 EPS miss -23.6%、财报后次日 -14.20%")
    assert attribution.check(answer, _records(
        ("explain_move", {"ticker": "SMCI"},
         "今日 -5.40% · 同期 SMH +2.90% → 相对强度 -8.30pp"),
        ("earnings_view", {"ticker": "SMCI"},
         "上次 EPS miss -23.6% · 财报后次日 -14.20%"))).ok


def test_attribution_allows_a_constituent_to_own_its_weight():
    """ARKK's card prints "TSLA 9.80%"; that weight is TSLA's as well as ARKK's."""
    from v2.agent import attribution

    assert attribution.check(
        "TSLA 在 ARKK 里占 9.80%。",
        _records(("etf_view", {"symbol": "ARKK"}, "前三：TSLA 9.80% · COIN 7.20%"))).ok


def _holders_registry() -> ToolRegistry:
    """Only NVDA has institutional data — the shape that produced h07."""
    def executor(spec, args):
        if args.get("ticker") == "NVDA":
            return "Vanguard 8.94% · BlackRock 7.31%"
        return "fixture 未记录该 ticker 的机构持仓。"

    return ToolRegistry(executor=executor)


def test_the_loop_repairs_a_misattributed_answer():
    llm = ScriptedLLM([
        _acts(_call("holders", 1, ticker="NVDA"), _call("holders", 2, ticker="AMD")),
        _says("NVDA Vanguard 8.94%；AMD Vanguard 8.94%。"),
        _says("NVDA 的 Vanguard 持股 8.94%；AMD 没有机构持仓数据。"),
    ])
    result = run_agent("我持仓的机构持股比例", llm=llm, registry=_holders_registry())
    assert result.repairs == 1
    assert result.stop_reason == "final_answer"
    assert result.attribution.ok


def test_an_unrepaired_misattribution_is_named_in_the_stop_reason():
    llm = ScriptedLLM([
        _acts(_call("holders", 1, ticker="NVDA"), _call("holders", 2, ticker="AMD")),
        _says("AMD Vanguard 8.94%。"),
        _says("AMD Vanguard 8.94%。"),          # repair fails too
    ])
    result = run_agent("机构持股", llm=llm, registry=_holders_registry())
    assert result.stop_reason == "final_answer_misattributed"
    assert not result.attribution.ok


def test_attribution_can_be_switched_off():
    llm = ScriptedLLM([
        _acts(_call("holders", 1, ticker="NVDA"), _call("holders", 2, ticker="AMD")),
        _says("AMD Vanguard 8.94%。"),
    ])
    result = run_agent("机构持股", llm=llm, registry=_holders_registry(),
                       config=AgentConfig(attribution_check=False))
    assert result.attribution.ok, "关掉后不应产生任何归属判定"
    assert result.repairs == 0


# ---------------------------------------------------------------------------
# context management
# ---------------------------------------------------------------------------

def test_clip_keeps_both_ends():
    clipped = clip("HEAD" + "m" * 500 + "TAIL", 100)
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert "elided" in clipped and len(clipped) < 200


def test_old_observations_are_compressed_but_recent_ones_are_not():
    llm = ScriptedLLM([
        _acts(_call("portfolio_view")),
        _acts(_call("risk_view")),
        _acts(_call("macro_view")),
        _acts(_call("earnings_view", 4, ticker="CRWD")),
        _says("CRWD 占仓 22.4%。"),
    ])
    result = run_agent("组合概况", llm=llm, registry=_registry(),
                       config=AgentConfig(max_steps=6, fresh_observations=2, stale_chars=200))

    assert result.trajectory.stats()["context_chars_saved"] > 0
    final_messages = llm.calls[-1]
    tool_messages = [m for m in final_messages if m["role"] == "tool"]
    assert any("elided" in m["content"] for m in tool_messages), "old observation clipped"
    assert not any("elided" in m["content"] for m in tool_messages[-2:]), "recent kept intact"


def test_notes_carry_forward_after_compression():
    llm = ScriptedLLM([
        _acts(_call("portfolio_view"), text="CRWD 是第一大持仓，占 22.4%。"),
        _acts(_call("risk_view")),
        _says("CRWD 占仓 22.4%。"),
    ])
    result = run_agent("最大持仓", llm=llm, registry=_registry())
    assert result.trajectory.notes
    assert any(m["role"] == "system" and "Interim findings" in m["content"]
               for m in llm.calls[-1])


# ---------------------------------------------------------------------------
# baseline — the incumbent, wired to the same registry
# ---------------------------------------------------------------------------

def test_baseline_makes_exactly_one_tool_call():
    classifier = ScriptedClassifier([{"intent": "risk_view", "ticker": ""}])
    result = run_baseline("我的组合风险怎么样", classifier=classifier, registry=_registry())

    assert result.tool == "risk_view"
    assert result.stats()["tool_calls"] == 1
    assert result.stats()["llm_calls"] == 1
    assert result.answer == PORTFOLIO_FIXTURES["risk_view"]  # verbatim, LLM writes nothing


def test_baseline_cannot_answer_a_multi_hop_question():
    """Same query the agent test uses: the router grabs one card and stops.

    risk_view does name the top holding, so the gap is not 'no ticker' — it is
    that ranking by *combined* risk needs facts that live in other tools. The
    single observation carries no earnings date and no insider activity, so any
    ranking the user reads into it would be unsupported.
    """
    classifier = ScriptedClassifier([{"intent": "risk_view", "ticker": ""}])
    result = run_baseline("我持仓里哪只最危险", classifier=classifier, registry=_registry())

    assert result.stats()["tool_calls"] == 1
    assert "2026-09-06" not in result.answer, "no earnings date — that is earnings_view"
    assert "Form 4" not in result.answer, "no insider evidence — that is insider_view"

    # The agent reaches those same facts on the same fixtures.
    llm = ScriptedLLM([
        _acts(_call("portfolio_view")),
        _acts(_call("earnings_view", 1, ticker="CRWD"),
              _call("insider_view", 2, ticker="CRWD")),
        _says("CRWD 最危险：占仓 22.4%，2026-09-06 财报，90 天内 3 次内部人卖出。"),
    ])
    agent = run_agent("我持仓里哪只最危险", llm=llm, registry=_registry())
    assert "2026-09-06" in agent.trajectory.observations_text()
    assert agent.trajectory.tool_calls > result.stats()["tool_calls"]


def test_baseline_unknown_intent_falls_back():
    classifier = ScriptedClassifier([{"intent": "unknown"}])
    result = run_baseline("今天天气", classifier=classifier, registry=_registry())
    assert result.tool == "" and "没听懂" in result.answer


def test_every_baseline_route_exists_in_the_registry():
    for intent_name, (tool_name, _) in INTENT_TO_TOOL.items():
        assert tool_name in SPECS_BY_NAME, f"{intent_name} routes to missing tool {tool_name}"


def test_baseline_covers_the_bots_intent_enum():
    """Mirrors v2/bot/intent.py's whitelist so drift shows up as a failure."""
    bot_intents = {
        "explain_move", "summary", "chain", "thirteen_f", "holders_view", "etf_view",
        "watchlist_view", "watchlist_add", "watchlist_remove", "settings",
        "find_anomalies", "alert_set", "alert_list", "portfolio_view", "pnl_view",
        "earnings_view", "earnings_calendar", "risk_view", "pnl_period",
        "eight_k_view", "insider_view", "macro_view", "release_check", "moneyflow_view",
    }
    from v2.agent.baseline import UNROUTED_INTENTS
    assert bot_intents - set(INTENT_TO_TOOL) == UNROUTED_INTENTS


# ---------------------------------------------------------------------------
# intent port — keeps the baseline runnable without LangChain
# ---------------------------------------------------------------------------

def test_intent_port_reads_the_live_bot_prompt():
    """Extraction must come from the bot's source, so the prompt cannot drift."""
    from v2.agent.intent_port import bot_intent_constants

    constants = bot_intent_constants()
    assert len(constants["_SYSTEM_PROMPT"]) > 500
    assert "意图分类器" in constants["_SYSTEM_PROMPT"]
    # 24 routable intents + "unknown"
    assert len(constants["_VALID_INTENTS"]) == 25
    assert set(INTENT_TO_TOOL) < constants["_VALID_INTENTS"]
    assert constants["_INSIDER_DAYS_BACK_MIN"] < constants["_INSIDER_DAYS_BACK_MAX"]


def test_intent_port_matches_classify_contract():
    from v2.agent.intent_port import classify

    parsed = classify("我的组合风险怎么样", llm=ScriptedLLM([
        _says('```json\n{"intent": "risk_view", "ticker": "", "raw": "组合风险"}\n```')]))
    assert parsed["intent"] == "risk_view"
    # every key the bot's classify promises its callers
    for key in ("intent", "ticker", "manager", "etf", "target_price", "direction",
                "days_horizon", "period", "days_back", "release_type", "raw"):
        assert key in parsed


def test_intent_port_coerces_outside_the_whitelist():
    from v2.agent.intent_port import classify

    assert classify("x", llm=ScriptedLLM([_says('{"intent": "buy_the_dip"}')]))["intent"] == "unknown"
    assert classify("x", llm=ScriptedLLM([_says("not json at all")]))["intent"] == "unknown"
    # bounded days_back, same clamping the bot applies
    parsed = classify("x", llm=ScriptedLLM([
        _says('{"intent": "insider_view", "ticker": "nvda", "days_back": 9999}')]))
    assert parsed["days_back"] == 365 and parsed["ticker"] == "NVDA"


def test_baseline_resolves_a_classifier_in_any_environment():
    from v2.agent.baseline import resolve_classifier

    classify, kind = resolve_classifier()
    assert callable(classify)
    assert kind in ("bot", "port")


# ---------------------------------------------------------------------------
# llm plumbing
# ---------------------------------------------------------------------------

def test_tool_argument_parsing_tolerates_fences_and_junk():
    assert _parse_arguments('```json\n{"ticker": "NVDA"}\n```')[0] == {"ticker": "NVDA"}
    assert _parse_arguments("")[0] == {}
    assert _parse_arguments("{ticker: NVDA")[1].startswith("invalid JSON")
    assert _parse_arguments("[1,2]")[1] == "arguments must be a JSON object"


def test_fixture_executor_records_calls():
    executor = FixtureExecutor(PORTFOLIO_FIXTURES)
    registry = ToolRegistry(executor=executor)
    registry.call("earnings_view", {"ticker": "CRWD"})
    assert executor.calls == [("earnings_view", {"ticker": "CRWD"})]


# ---------------------------------------------------------------------------
# standalone runner (no pytest in the sandbox)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
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
