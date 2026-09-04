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
