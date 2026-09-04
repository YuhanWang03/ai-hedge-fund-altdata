"""Tests for B1 — the anomaly evidence top-up.

This runs unattended inside a cron, which flips the priorities: the assertions
that matter most are the ones about *not* doing something. It must not touch
anomalies that are already explained, must not exceed its per-run cap, must not
let a hung tool hold the cron open, and must not add a reason it cannot ground.
Every one of those is a way an unattended agent quietly makes a product worse.
"""

from __future__ import annotations

import pathlib
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import copy  # noqa: E402
import json  # noqa: E402

from v2.agent import anomaly_assist as A  # noqa: E402
from v2.agent.fixtures import build_anomaly_registry  # noqa: E402
from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall  # noqa: E402
from v2.agent.samples import ANOMALY_CASES, SimpleAnomaly, SimpleReason  # noqa: E402


def _cfg(**kwargs) -> A.AssistConfig:
    defaults = {"deadline_seconds": 10.0, "hard_deadline_grace": 2.0}
    defaults.update(kwargs)
    return A.AssistConfig(**defaults)


def _call(index: int, name: str, ticker: str) -> ToolCall:
    return ToolCall(id=f"c{index}", name=name, arguments={"ticker": ticker},
                    raw_arguments=json.dumps({"ticker": ticker}))


def _answers(payload) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False),
                       prompt_tokens=100, completion_tokens=20)


def _arm() -> SimpleAnomaly:
    return SimpleAnomaly("ARM", 0.0742, 3.4, ["volume_spike", "52w_high"], True, [])


# ---------------------------------------------------------------------------
# eligibility and budgeting
# ---------------------------------------------------------------------------

def test_explained_anomalies_are_left_alone():
    """The normal path must cost nothing — this is the whole 'failure branch only' claim."""
    assert not A.needs_assist(SimpleAnomaly("NVDA", 0.03, 1.8, [], False,
                                            [SimpleReason("签署供货框架", "高")]))
    assert not A.needs_assist(SimpleAnomaly("MSFT", 0.01, 1.2, [], False,
                                            [SimpleReason("大盘普涨", "中")]))


def test_unexplained_shapes_both_qualify():
    assert A.needs_assist(SimpleAnomaly("ARM", 0.07, 3.4, [], False, []))
    assert A.needs_assist(SimpleAnomaly("PLTR", -0.05, 2.9, [], False,
                                        [SimpleReason("板块回调", "低"),
                                         SimpleReason("传闻", "低")]))


def test_per_run_cap_holds_and_ranks_by_move_size():
    selected = A.select_candidates(list(ANOMALY_CASES), _cfg(max_items_per_run=3))
    assert [a.ticker for a in selected] == ["ARM", "SMCI", "PLTR"]
    assert "AMD" not in {a.ticker for a in selected}, "小波动被挤出上限，原样推送"
    assert "NVDA" not in {a.ticker for a in selected}, "已解释的从不进入候选"


def test_cap_of_zero_disables_the_feature_entirely():
    assert A.select_candidates(list(ANOMALY_CASES), _cfg(max_items_per_run=0)) == []


def test_contrarian_moves_outrank_equal_sized_ones():
    plain = SimpleAnomaly("AAA", 0.05, 2.0, ["volume_spike"], False, [])
    against = SimpleAnomaly("BBB", 0.05, 2.0, ["volume_spike"], True, [])
    assert A.candidate_score(against) > A.candidate_score(plain)


# ---------------------------------------------------------------------------
# tool surface
# ---------------------------------------------------------------------------

def test_only_the_four_read_only_tools_are_reachable():
    registry = build_anomaly_registry()
    assert set(registry.names()) == set(A.ASSIST_TOOLS)
    blocked = registry.call("portfolio_view", {})
    assert not blocked.ok and blocked.error_kind == "unknown_tool"


def test_writes_stay_blocked():
    registry = build_anomaly_registry()
    result = registry.call("watchlist_add", {"ticker": "ARM"})
    assert not result.ok


# ---------------------------------------------------------------------------
# parsing — unattended output must be structured, not prose
# ---------------------------------------------------------------------------

def test_parses_a_fenced_json_answer():
    reasons, error = A._parse_reasons(
        '```json\n{"reasons": [{"text": "签了 $2.40B 合同", '
        '"confidence": "高", "evidence_tool": "eight_k_view"}]}\n```')
    assert not error and len(reasons) == 1
    assert reasons[0].confidence == "高"
    assert reasons[0].note() == "agent 补齐 · eight_k_view"


def test_confidence_outside_the_enum_is_coerced():
    reasons, _ = A._parse_reasons('{"reasons": [{"text": "x", "confidence": "极高"}]}')
    assert reasons[0].confidence == "中", "沿用 bot 的枚举降级姿态"


def test_a_tool_name_the_agent_never_had_is_dropped():
    reasons, _ = A._parse_reasons(
        '{"reasons": [{"text": "x", "evidence_tool": "portfolio_view"}]}')
    assert reasons[0].evidence_tool == ""


def test_prose_instead_of_json_is_rejected():
    reasons, error = A._parse_reasons("我觉得是因为大盘不好。")
    assert not reasons and error


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------

def test_successful_top_up_is_grounded_and_applied():
    anomaly = _arm()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[_call(1, "eight_k_view", "ARM")]),
        _answers({"reasons": [{
            "text": "8-K Item 1.01：签署 5 年、总额 $2.40B 的架构授权协议",
            "confidence": "高", "evidence_tool": "eight_k_view"}]}),
    ])
    outcome = A.assist(anomaly, llm=llm, registry=build_anomaly_registry(), config=_cfg())

    assert outcome.ok and outcome.outcome == "ok"
    assert outcome.grounding_ratio == 1.0
    assert A.apply(anomaly, outcome, factory=SimpleReason) == 1
    assert anomaly.reasons[0].confidence == "高"
    assert "agent 补齐" in anomaly.reasons[0].note, "补齐的来源必须对读者可见"
    assert not A.needs_assist(anomaly)


def test_honest_no_finding_adds_nothing():
    anomaly = _arm()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[_call(1, "earnings_view", "ARM")]),
        _answers({"reasons": []}),
    ])
    outcome = A.assist(anomaly, llm=llm, registry=build_anomaly_registry(), config=_cfg())

    assert outcome.outcome == "no_finding" and not outcome.ok
    assert A.apply(anomaly, outcome, factory=SimpleReason) == 0
    assert anomaly.reasons == []


def test_ungrounded_reason_is_discarded_not_repaired():
    """No one is watching, so an unsupported figure is dropped, not negotiated."""
    anomaly = _arm()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[_call(1, "eight_k_view", "ARM")]),
        _answers({"reasons": [{"text": "机构持仓较上季度下降 31.5%",
                               "confidence": "中", "evidence_tool": "eight_k_view"}]}),
    ])
    outcome = A.assist(anomaly, llm=llm, registry=build_anomaly_registry(), config=_cfg())

    assert outcome.outcome == "ungrounded"
    assert "31.5" in outcome.detail
    assert anomaly.reasons == []


def test_a_failing_tool_does_not_abort_the_top_up():
    """SMCI's 8-K fixture raises; the loop must route around it as usual."""
    anomaly = SimpleAnomaly("SMCI", -0.054, 3.1, ["52w_low"], True,
                            [SimpleReason("渠道调研", "低")])
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[_call(1, "eight_k_view", "SMCI"),
                                _call(2, "earnings_view", "SMCI")]),
        _answers({"reasons": []}),
    ])
    outcome = A.assist(anomaly, llm=llm, registry=build_anomaly_registry(), config=_cfg())
    assert outcome.outcome == "no_finding"
    assert outcome.tool_calls == 2


def test_llm_failure_returns_an_outcome_rather_than_raising():
    class Exploding:
        def complete(self, messages, tools=None):
            raise RuntimeError("provider down")

    anomaly = _arm()
    outcome = A.assist(anomaly, llm=Exploding(), registry=build_anomaly_registry(),
                       config=_cfg())
    # run_agent turns LLMError into a stop reason; anything else surfaces as "error".
    assert outcome.outcome in ("error", "unparsable", "no_finding")
    assert not outcome.ok and anomaly.reasons == []


# ---------------------------------------------------------------------------
# the deadline — the cron must always terminate
# ---------------------------------------------------------------------------

def test_deadline_abandons_a_hung_worker():
    def hangs():
        time.sleep(5)
        return "never"

    started = time.time()
    value, timed_out = A.run_with_deadline(hangs, 0.05)
    assert timed_out and value is None
    assert time.time() - started < 1.0, "必须立刻返回，不能等 worker"


def test_deadline_propagates_a_real_error():
    def explodes():
        raise ValueError("boom")

    try:
        A.run_with_deadline(explodes, 1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("worker 的异常必须传回来，不能被吞掉")


def test_timeout_leaves_the_anomaly_untouched():
    class Slow:
        def complete(self, messages, tools=None):
            time.sleep(2)
            return LLMResponse(text="{}")

    anomaly = _arm()
    outcome = A.assist(anomaly, llm=Slow(), registry=build_anomaly_registry(),
                       config=_cfg(deadline_seconds=0.05, hard_deadline_grace=0.05))
    assert outcome.outcome == "timeout"
    assert anomaly.reasons == []


# ---------------------------------------------------------------------------
# batch + flag
# ---------------------------------------------------------------------------

def test_batch_applies_successes_and_leaves_the_rest_verbatim():
    anomalies = [copy.deepcopy(a) for a in ANOMALY_CASES]
    before = {a.ticker: list(a.reasons) for a in anomalies}

    llm = ScriptedLLM([
        LLMResponse(tool_calls=[_call(1, "eight_k_view", "ARM")]),
        _answers({"reasons": [{"text": "签署 $2.40B 架构授权协议", "confidence": "高",
                               "evidence_tool": "eight_k_view"}]}),
        LLMResponse(tool_calls=[_call(2, "earnings_view", "SMCI")]),
        _answers({"reasons": []}),
        LLMResponse(tool_calls=[_call(3, "insider_view", "PLTR")]),
        _answers({"reasons": [{"text": "机构持仓下降 31.5%", "confidence": "中"}]}),
    ])
    outcomes = assist_outcomes = A.assist_batch(
        anomalies, llm=llm, registry=build_anomaly_registry(),
        config=_cfg(), factory=SimpleReason)

    assert [o.outcome for o in assist_outcomes] == ["ok", "no_finding", "ungrounded"]
    by_ticker = {a.ticker: a for a in anomalies}
    assert len(by_ticker["ARM"].reasons) == 1
    for ticker in ("SMCI", "PLTR", "NVDA", "MSFT", "AMD"):
        assert by_ticker[ticker].reasons == before[ticker], f"{ticker} 必须原样不动"
    assert len(outcomes) == 3, "每轮最多 3 条，AMD 不参与"


def test_single_pass_helper_selects_the_same_set_as_ranked_batching():
    """The cron keeps its one-pass loop; ordering up front makes that equivalent."""
    anomalies = [copy.deepcopy(a) for a in ANOMALY_CASES]
    expected = {a.ticker for a in A.select_candidates(anomalies, _cfg())}

    assistant = A.BudgetedAssistant(_cfg(), llm=ScriptedLLM([]),
                                    registry=build_anomaly_registry(),
                                    factory=SimpleReason, force=True)
    attempted = []
    for anomaly in assistant.order(anomalies):
        if assistant.maybe_assist(anomaly) is not None:
            attempted.append(anomaly.ticker)

    assert set(attempted) == expected
    assert assistant.remaining == 0


def test_helper_is_inert_when_the_flag_is_off():
    anomalies = [copy.deepcopy(a) for a in ANOMALY_CASES]
    assistant = A.BudgetedAssistant(_cfg(), registry=build_anomaly_registry())
    for anomaly in assistant.order(anomalies):
        assert assistant.maybe_assist(anomaly) is None
    assert assistant.summary()["attempted"] == 0


def test_helper_summary_counts_outcomes():
    anomaly = _arm()
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[_call(1, "eight_k_view", "ARM")]),
        _answers({"reasons": [{"text": "签署 $2.40B 架构授权协议", "confidence": "高",
                               "evidence_tool": "eight_k_view"}]}),
    ])
    assistant = A.BudgetedAssistant(_cfg(), llm=llm, registry=build_anomaly_registry(),
                                    factory=SimpleReason, force=True)
    assistant.maybe_assist(anomaly)
    summary = assistant.summary()
    assert summary["applied"] == 1 and summary["by_outcome"] == {"ok": 1}


def test_flag_is_off_by_default():
    import os
    saved = os.environ.pop("V2_AGENT_ANOMALY_ASSIST", None)
    try:
        assert A.enabled() is False
        os.environ["V2_AGENT_ANOMALY_ASSIST"] = "true"
        assert A.enabled() is True
    finally:
        os.environ.pop("V2_AGENT_ANOMALY_ASSIST", None)
        if saved is not None:
            os.environ["V2_AGENT_ANOMALY_ASSIST"] = saved


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
