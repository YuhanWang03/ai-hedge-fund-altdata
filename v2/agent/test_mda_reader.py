"""Tests for B2 — the MD&A reading layer.

The layer's whole claim is "additive and verifiable", so the assertions are
mostly about restraint: a fabricated quote is dropped rather than softened, a
quarter with nothing new costs nothing, and the deterministic fields the card
already renders are never touched no matter what the model returns.

The clipping test is the subtle one. Paragraphs are truncated before they are
sent, so verification runs against the truncated corpus — otherwise a quote could
"verify" against filing text the model never saw and could only have guessed.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent import mda_reader as M  # noqa: E402
from v2.agent.llm import LLMResponse, ScriptedLLM  # noqa: E402
from v2.agent.samples import MDA_CASES, SimpleTenQDelta  # noqa: E402


def _cfg(**kwargs) -> M.ReaderConfig:
    defaults = {"deadline_seconds": 5.0, "hard_deadline_grace": 2.0}
    defaults.update(kwargs)
    return M.ReaderConfig(**defaults)


def _says(payload) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False),
                       prompt_tokens=100, completion_tokens=50)


def _crwd() -> SimpleTenQDelta:
    return copy.deepcopy(MDA_CASES["CRWD"])


# ---------------------------------------------------------------------------
# cost control
# ---------------------------------------------------------------------------

def test_no_new_paragraphs_means_no_call_at_all():
    llm = ScriptedLLM([])          # any call would be visible in llm.calls
    outcome = M.read(MDA_CASES["MSFT"], llm=llm, config=_cfg())
    assert outcome.outcome == "nothing_to_read"
    assert llm.calls == [], "本季无新增段落时不该发生任何调用"
    assert outcome.tokens == 0


def test_only_the_configured_number_of_paragraphs_is_sent():
    delta = SimpleTenQDelta("X", "Q1", ["p one", "p two", "p three", "p four"])
    corpus = M.build_corpus(delta, _cfg(max_paragraphs=2))
    assert corpus == ["p one", "p two"]


# ---------------------------------------------------------------------------
# verification — the point of the whole layer
# ---------------------------------------------------------------------------

def test_verbatim_quote_is_accepted():
    outcome = M.read(_crwd(), config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "extended customer acceptance cycles",
        "reading": "企业客户签约到确认收入的周期在拉长", "direction": "利空"}]})]))
    assert outcome.ok and outcome.points[0].direction == "利空"
    assert not outcome.rejected


def test_fabricated_quote_is_dropped_not_softened():
    """The dangerous case: a reassuring reading of a going-concern filing."""
    delta = copy.deepcopy(MDA_CASES["BADCO"])
    outcome = M.read(delta, config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "management expects liquidity to normalize by year end",
        "reading": "管理层预计年底流动性恢复正常", "direction": "利好"}]})]))

    assert outcome.outcome == "unquoted" and not outcome.ok
    assert outcome.rejected and "引用不在原文中" in outcome.rejected[0]
    assert outcome.render() == "", "被拒时不能渲染出任何内容"


def test_partial_rejection_keeps_the_verifiable_half():
    outcome = M.read(_crwd(), config=_cfg(), llm=ScriptedLLM([_says({"points": [
        {"quote": "extended customer acceptance cycles", "reading": "周期拉长",
         "direction": "利空"},
        {"quote": "management is confident in a strong rebound", "reading": "管理层看好反弹",
         "direction": "利好"},
    ]})]))
    assert outcome.ok and len(outcome.points) == 1
    assert len(outcome.rejected) == 1


def test_whitespace_reflow_still_matches():
    """Models re-wrap long quotes; that must not count as a fabrication."""
    delta = SimpleTenQDelta("X", "Q1", ["we observed extended customer\nacceptance cycles"])
    outcome = M.read(delta, config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "extended customer   acceptance cycles", "reading": "周期拉长"}]})]))
    assert outcome.ok


def test_a_quote_too_short_to_prove_anything_is_rejected():
    delta = SimpleTenQDelta("X", "Q1", ["the company continues to invest"])
    outcome = M.read(delta, config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "the", "reading": "看多"}]})]))
    assert outcome.outcome == "unquoted"
    assert "过短" in outcome.rejected[0]


def test_verification_uses_the_clipped_corpus_not_the_full_filing():
    """A quote from text that was truncated away was never seen — so it cannot verify."""
    tail = "this sentence lives past the clip boundary"
    delta = SimpleTenQDelta("X", "Q1", ["A" * 200 + tail])
    outcome = M.read(delta, config=_cfg(max_paragraph_chars=100),
                     llm=ScriptedLLM([_says({"points": [{
                         "quote": tail, "reading": "解读"}]})]))
    assert outcome.outcome == "unquoted"


def test_numbers_in_the_reading_are_still_checked():
    outcome = M.read(_crwd(), config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "extended customer acceptance cycles",
        "reading": "周期拉长，预计影响下季营收 12.7%", "direction": "利空"}]})]))
    assert outcome.outcome == "ungrounded"
    assert "12.7" in outcome.detail


def test_a_figure_quoted_from_the_filing_passes():
    outcome = M.read(_crwd(), config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "a charge of $18.4 million related to the restructuring of",
        "reading": "重组已计提 $18.4 million", "direction": "利空"}]})]))
    assert outcome.ok


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_empty_points_is_a_legitimate_answer():
    outcome = M.read(MDA_CASES["AAPL"], config=_cfg(),
                     llm=ScriptedLLM([_says({"points": []})]))
    assert outcome.outcome == "no_finding" and not outcome.ok


def test_direction_outside_the_enum_is_coerced():
    points, _ = M.parse_points('{"points": [{"quote": "abcdefghij", '
                               '"reading": "x", "direction": "非常利空"}]}')
    assert points[0].direction == "中性"


def test_fenced_json_and_smart_quotes_are_handled():
    points, error = M.parse_points(
        '```json\n{"points": [{"quote": "「abcdefghij」", "reading": "x"}]}\n```')
    assert not error and points[0].quote == "abcdefghij"


def test_prose_is_rejected():
    outcome = M.read(_crwd(), config=_cfg(),
                     llm=ScriptedLLM([LLMResponse(text="我觉得这段挺重要的。")]))
    assert outcome.outcome == "unparsable"


# ---------------------------------------------------------------------------
# resilience — this runs inside the 21:00 cron
# ---------------------------------------------------------------------------

def test_timeout_produces_an_outcome_not_a_hang():
    class Slow:
        def complete(self, messages, tools=None):
            time.sleep(2)
            return LLMResponse(text="{}")

    started = time.time()
    outcome = M.read(_crwd(), llm=Slow(),
                     config=_cfg(deadline_seconds=0.05, hard_deadline_grace=0.05))
    assert outcome.outcome == "timeout"
    assert time.time() - started < 1.0


def test_provider_failure_is_caught():
    class Exploding:
        def complete(self, messages, tools=None):
            raise RuntimeError("provider down")

    outcome = M.read(_crwd(), llm=Exploding(), config=_cfg())
    assert outcome.outcome == "error" and not outcome.ok


def test_the_deterministic_delta_is_never_mutated():
    delta = _crwd()
    before = copy.deepcopy(delta)
    M.read(delta, config=_cfg(), llm=ScriptedLLM([_says({"points": [{
        "quote": "extended customer acceptance cycles", "reading": "周期拉长"}]})]))
    assert delta == before, "解读层只做加法，绝不修改确定性管线的产出"


# ---------------------------------------------------------------------------
# cron wrapper
# ---------------------------------------------------------------------------

def test_wrapper_is_inert_without_the_flag():
    import os
    saved = os.environ.pop("V2_AGENT_EARNINGS_READ", None)
    try:
        assert M.enabled() is False
        assert M.read_if_enabled(_crwd()) is None
        assert M.read_if_enabled(None) is None
        os.environ["V2_AGENT_EARNINGS_READ"] = "true"
        assert M.enabled() is True
        assert M.read_if_enabled(None) is None, "没有 10-Q diff 时依然是 no-op"
    finally:
        os.environ.pop("V2_AGENT_EARNINGS_READ", None)
        if saved is not None:
            os.environ["V2_AGENT_EARNINGS_READ"] = saved


def test_render_is_empty_unless_the_reading_survived():
    assert M.ReadingOutcome("X", outcome="unquoted").render() == ""
    ok = M.ReadingOutcome("X", points=[M.ReadingPoint("abcdefghij", "解读", "利空")])
    assert "MD&A 措辞解读" in ok.render() and "🔻" in ok.render()


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
