"""Tests for the persona package. No network, no API key, no LLM."""

from __future__ import annotations

import json

import pytest

from v2.agent.llm import LLMResponse, ScriptedLLM
from v2.personas import PERSONAS, get_persona, list_personas
from v2.personas.base import apply_margin_of_safety, classic_verdict, confidence_from_ratio
from v2.personas.committee import analyze_snapshot, run_committee, tally
from v2.personas.data import ALL_LINE_ITEMS, FinancialDatasetsClient, adapt_client
from v2.personas.fixtures import distressed_snapshot, empty_snapshot, quality_snapshot
from v2.personas.models import Evaluation, PersonaSignal, Record, SubScore, as_records
from v2.personas.narrate import build_messages, narrate, parse_reply
from v2.personas.snapshot import PersonaSnapshot, build_snapshot


# --------------------------------------------------------------------------- models

def test_record_missing_field_reads_none_and_supports_mapping_protocol():
    r = Record({"a": 1}, b=None)
    assert r.a == 1 and r.b is None and r.zzz is None
    assert hasattr(r, "zzz")  # documented: never rely on AttributeError
    assert "a" in r and len(r) == 2 and r.get("q", 7) == 7
    assert r.model_dump() == {"a": 1, "b": None}
    r.c = 3
    assert r.to_dict()["c"] == 3


def test_as_records_accepts_dicts_records_and_objects():
    class Obj:
        def __init__(self):
            self.x = 1
            self._hidden = 2

    class Model:
        def model_dump(self):
            return {"y": 2}

    rows = as_records([{"a": 1}, Record(b=2), Obj(), Model()])
    assert [r.to_dict() for r in rows] == [{"a": 1}, {"b": 2}, {"x": 1}, {"y": 2}]


def test_evaluation_ratio_and_signal_serialization():
    ev = Evaluation(parts=[SubScore("a", 3, 5), SubScore("b", 1, 5)])
    assert ev.score == 4 and ev.max_score == 10 and ev.ratio == 0.4
    sig = PersonaSignal(persona="p", ticker="T", as_of="2026-01-01", signal="bullish", confidence=80, score=4, max_score=10, parts=ev.parts)
    body = json.loads(json.dumps(sig.to_dict()))
    assert body["ratio"] == 0.4 and body["parts"][0]["name"] == "a" and sig.direction == 1


# ----------------------------------------------------------------------------- base

def test_classic_verdict_and_confidence_are_monotone():
    assert classic_verdict(0.7) == "bullish" and classic_verdict(0.3) == "bearish" and classic_verdict(0.5) == "neutral"
    assert confidence_from_ratio(1.0, "bullish") > confidence_from_ratio(0.7, "bullish") >= 55
    assert confidence_from_ratio(0.0, "bearish") > confidence_from_ratio(0.3, "bearish") >= 55
    assert confidence_from_ratio(0.5, "neutral") == 50
    assert 10 <= confidence_from_ratio(0.31, "neutral") < 50


def test_margin_of_safety_gate():
    assert apply_margin_of_safety("bullish", 80, None) == ("bullish", 80)
    assert apply_margin_of_safety("bullish", 80, -0.1)[0] == "neutral"
    assert apply_margin_of_safety("neutral", 45, -0.4)[0] == "bearish"
    assert apply_margin_of_safety("bullish", 80, 0.5) == ("bullish", 85)


# ------------------------------------------------------------------------- snapshot

def test_snapshot_hash_depends_on_data_not_fetch_time():
    a, b = quality_snapshot(), quality_snapshot()
    b.fetched_at = "2030-01-01T00:00:00"
    assert a.content_hash == b.content_hash
    b.market_cap = 1.0
    assert a.content_hash != b.content_hash


def test_snapshot_round_trips_through_dict():
    snap = quality_snapshot()
    again = PersonaSnapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert again.content_hash == snap.content_hash
    assert again.metrics("ttm", 3)[0].return_on_equity == snap.metrics_ttm[0].return_on_equity
    assert len(again.prices) == len(snap.prices)


class _FakeClient:
    """Speaks the persona protocol and counts what was asked of it."""

    def __init__(self, snap: PersonaSnapshot):
        self.snap = snap
        self.calls: list[str] = []

    def get_financial_metrics(self, ticker, end_date, *, period="ttm", limit=10):
        self.calls.append(f"metrics:{period}")
        return [r.to_dict() for r in self.snap.metrics(period, limit)]

    def search_line_items(self, ticker, line_items, end_date, *, period="ttm", limit=10):
        self.calls.append(f"items:{period}")
        assert set(line_items) == set(ALL_LINE_ITEMS)
        return [r.to_dict() for r in self.snap.line_items(period, limit)]

    def get_market_cap(self, ticker, end_date):
        self.calls.append("market_cap")
        return self.snap.market_cap

    def get_insider_trades(self, ticker, end_date, *, start_date=None, limit=1000):
        self.calls.append("insiders")
        return list(self.snap.insider_trades)

    def get_company_news(self, ticker, end_date, *, start_date=None, limit=100):
        self.calls.append("news")
        return list(self.snap.news)

    def get_prices(self, ticker, start_date, end_date):
        self.calls.append("prices")
        return list(reversed(self.snap.prices))  # deliberately unsorted


def test_build_snapshot_fetches_once_and_only_what_is_needed():
    client = _FakeClient(quality_snapshot())
    snap = build_snapshot("qlty", "2026-06-30", client, need=("prices",))
    assert snap.ticker == "QLTY" and snap.market_cap == 250_000e6
    assert client.calls.count("metrics:ttm") == 1 and client.calls.count("items:annual") == 1
    assert "insiders" not in client.calls and "news" not in client.calls and "prices" in client.calls
    assert snap.prices[0].time < snap.prices[-1].time  # re-sorted ascending
    assert snap.gaps == []


def test_build_snapshot_records_gaps_instead_of_raising():
    class Broken:
        def get_financial_metrics(self, ticker, end_date, *, period="ttm", limit=10):
            raise RuntimeError("boom")

        def search_line_items(self, *a, **k):
            return []

        def get_market_cap(self, *a):
            return None

        def get_insider_trades(self, *a, **k):
            return []

        def get_company_news(self, *a, **k):
            return []

        def get_prices(self, *a):
            return []

    snap = build_snapshot("X", "2026-06-30", Broken())
    assert any(g.startswith("metrics_ttm: RuntimeError") for g in snap.gaps)
    assert any(g.startswith("fundamentals:") for g in snap.gaps)
    assert not snap.has_fundamentals


def test_adapt_client_wraps_a_production_style_client(monkeypatch):
    monkeypatch.delenv("FINANCIAL_DATASETS_API_KEY", raising=False)

    class ProdLike:
        """Positional signature like the VPS FDClient, no line-item support."""

        def get_financial_metrics(self, ticker, end_date, limit=1):
            return [{"market_cap": 42.0, "return_on_equity": 0.2}]

        def get_prices(self, ticker, start, end):
            return [{"time": "2026-01-02", "close": 1.0}]

    fd = adapt_client(ProdLike())
    assert fd.get_financial_metrics("T", "2026-06-30", period="ttm", limit=5)[0].return_on_equity == 0.2
    assert fd.get_market_cap("T", "2026-06-30") == 42.0
    with pytest.raises(NotImplementedError):
        fd.search_line_items("T", ["revenue"], "2026-06-30")
    snap = build_snapshot("T", "2026-06-30", ProdLike(), need=("prices",))
    assert snap.market_cap == 42.0 and snap.metrics_ttm and snap.prices
    assert any(g.startswith("line_items_ttm") for g in snap.gaps)


def test_adapt_client_returns_protocol_clients_unchanged():
    client = _FakeClient(quality_snapshot())
    assert adapt_client(client) is client
    http = FinancialDatasetsClient("k")
    assert adapt_client(http) is http and http.api_key == "k"


# ------------------------------------------------------------------------- personas

@pytest.mark.parametrize("key", PERSONAS)
def test_every_persona_runs_deterministically(key):
    p = get_persona(key)
    assert p.key == key and p.name and p.name_zh and p.style and p.system_prompt
    for snap in (quality_snapshot(), distressed_snapshot()):
        a = p.analyze(snap)
        b = p.analyze(snap)
        assert a.signal in ("bullish", "bearish", "neutral") and 0 <= a.confidence <= 100
        assert a.to_dict() == b.to_dict(), "persona output must be deterministic"
        assert a.max_score > 0 and a.parts and a.snapshot_hash == snap.content_hash
        assert abs(sum(part.score for part in a.parts) - a.score) < 1e-9
        json.dumps(a.to_dict())  # serializable end to end


@pytest.mark.parametrize("key", PERSONAS)
def test_every_persona_abstains_without_data(key):
    s = get_persona(key).analyze(empty_snapshot())
    assert s.abstained and s.confidence == 0 and s.signal == "neutral"
    assert s.reasoning.startswith("abstain")


@pytest.mark.parametrize("key", PERSONAS)
def test_no_persona_is_bullish_on_the_distressed_company(key):
    s = get_persona(key).analyze(distressed_snapshot())
    assert s.signal != "bullish", s.reasoning


def test_buffett_needs_a_margin_of_safety_to_be_bullish():
    buffett = get_persona("warren_buffett")
    rich = quality_snapshot()
    rich_signal = buffett.analyze(rich)
    assert rich_signal.margin_of_safety is not None and rich_signal.margin_of_safety < 0
    assert rich_signal.signal == "neutral"
    cheap = quality_snapshot()
    cheap.market_cap = 60_000e6
    cheap_signal = buffett.analyze(cheap)
    assert cheap_signal.margin_of_safety > 0 and cheap_signal.signal == "bullish"
    assert cheap_signal.confidence > rich_signal.confidence
    assert "moat" in {p.name for p in cheap_signal.parts}


def test_list_personas_orders_and_caches():
    people = list_personas(["ben_graham", "warren_buffett"])
    assert [p.key for p in people] == ["ben_graham", "warren_buffett"]
    assert get_persona("ben_graham") is people[0]
    with pytest.raises(KeyError):
        get_persona("nobody")


# ------------------------------------------------------------------------ committee

def _sig(persona, signal, conf, abstained=False):
    return PersonaSignal(persona=persona, ticker="T", as_of="2026-06-30", signal=signal, confidence=conf, score=1, max_score=2, abstained=abstained)


def test_tally_weights_votes_by_confidence_and_ignores_abstentions():
    v = tally([_sig("a", "bullish", 80), _sig("b", "bullish", 60), _sig("c", "bearish", 40), _sig("d", "neutral", 50), _sig("e", "neutral", 0, abstained=True)])
    assert (v.bullish, v.bearish, v.neutral, v.abstained) == (2, 1, 1, 1)
    assert v.net_votes == 1 and v.voters == 4
    assert v.consensus == pytest.approx((80 + 60 - 40) / 400)
    assert v.agreement == 0.5 and v.avg_confidence == pytest.approx(57.5)
    assert v.stance == "bullish"
    assert tally([]).stance == "abstain"


def test_run_committee_on_prebuilt_snapshots_ranks_quality_over_distress():
    snaps = {s.ticker: s for s in (quality_snapshot(), distressed_snapshot())}
    result = run_committee(["dstr", "QLTY", "qlty"], personas=["warren_buffett", "ben_graham", "michael_burry"], snapshots=snaps)
    assert [v.ticker for v in result.verdicts] == ["QLTY", "DSTR"]
    assert result.verdicts[0].rank == 1 and result.verdicts[0].consensus > result.verdicts[1].consensus
    assert result.verdict("DSTR").stance == "bearish"
    grid = result.matrix()
    assert set(grid) == {"warren_buffett", "ben_graham", "michael_burry"} and set(grid["ben_graham"]) == {"QLTY", "DSTR"}
    assert [v.ticker for v in result.top(1)] == ["QLTY"]
    json.dumps(result.to_dict())


def test_run_committee_uses_client_for_missing_snapshots_and_reports_errors():
    client = _FakeClient(quality_snapshot())
    result = run_committee(["QLTY"], client, personas=["warren_buffett"], max_workers=2)
    assert result.verdicts and result.verdicts[0].ticker == "QLTY" and not result.errors
    assert "insiders" not in client.calls  # Buffett does not read insider trades

    class Exploding:
        def get_financial_metrics(self, *a, **k):
            raise ValueError("no")

    result = run_committee(["ZZZ"], Exploding(), personas=["warren_buffett"])
    # An exploding client degrades to gaps, never to an exception.
    assert result.verdicts[0].signals[0].abstained


def test_a_crashing_persona_abstains_instead_of_sinking_the_vote():
    class Broken(type(get_persona("warren_buffett"))):
        key = "broken"

        def evaluate(self, snap):
            raise ZeroDivisionError("bad math")

    v = analyze_snapshot(quality_snapshot(), [get_persona("warren_buffett"), Broken()])
    assert v.abstained == 1 and v.voters == 1
    assert "ZeroDivisionError" in v.signals[1].reasoning


# --------------------------------------------------------------------------- narrate

def _bullish_signal():
    cheap = quality_snapshot()
    cheap.market_cap = 60_000e6
    return get_persona("warren_buffett").analyze(cheap)


def test_parse_reply_handles_fences_and_prose():
    assert parse_reply('```json\n{"signal":"bullish","confidence":70,"reasoning":"ok"}\n```')["reasoning"] == "ok"
    assert parse_reply('Sure: {"signal":"bullish","confidence":70,"reasoning":"ok"}.')["confidence"] == 70
    assert parse_reply("no json here") is None


def test_narrate_keeps_a_grounded_reply_and_flags_invented_numbers():
    sig = _bullish_signal()
    roe = sig.parts[0].details  # "Strong ROE of 24.0%; ..."
    good = json.dumps({"signal": sig.signal, "confidence": sig.confidence, "reasoning": f"护城河扎实，{roe.split(';')[0]}，安全边际为正。"})
    llm = ScriptedLLM([LLMResponse(text=good)])
    out = narrate(sig, llm=llm)
    assert out.narrative and out.narrative_grounded is True
    messages = llm.calls[0]
    assert messages[0]["role"] == "system" and "Warren Buffett" in messages[0]["content"]
    assert str(sig.confidence) in messages[1]["content"]

    sig2 = _bullish_signal()
    invented = json.dumps({"signal": sig2.signal, "confidence": sig2.confidence, "reasoning": "ROE 高达 87.5%，市盈率仅 3.14 倍。"})
    narrate(sig2, llm=ScriptedLLM([LLMResponse(text=invented)]))
    assert sig2.narrative and sig2.narrative_grounded is False


def test_narrate_discards_replies_that_change_the_verdict_or_fail():
    sig = _bullish_signal()
    flipped = json.dumps({"signal": "bearish", "confidence": sig.confidence, "reasoning": "no"})
    assert narrate(sig, llm=ScriptedLLM([LLMResponse(text=flipped)])).narrative is None
    changed = json.dumps({"signal": sig.signal, "confidence": 1, "reasoning": "no"})
    assert narrate(sig, llm=ScriptedLLM([LLMResponse(text=changed)])).narrative is None
    assert narrate(sig, llm=ScriptedLLM([LLMResponse(text="garbage")])).narrative is None

    class Dead:
        def complete(self, messages, tools=None):
            raise RuntimeError("offline")

    assert narrate(sig, llm=Dead()).narrative is None
    skipped = get_persona("warren_buffett").analyze(empty_snapshot())
    assert narrate(skipped, llm=Dead()).narrative is None  # abstentions are never narrated


def test_build_messages_mentions_language_and_facts():
    sig = _bullish_signal()
    zh = build_messages(sig, get_persona("warren_buffett"), language="zh")
    en = build_messages(sig, get_persona("warren_buffett"), language="en")
    assert "Simplified Chinese" in zh[0]["content"] and "English" in en[0]["content"]
    assert '"ticker":"QLTY"' in zh[1]["content"]


# -------------------------------------------------------------------------------- cli

def test_cli_demo_runs_without_network(capsys):
    from v2.personas.__main__ import main

    assert main(["--demo", "--personas", "warren_buffett,ben_graham", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert [v["ticker"] for v in body["verdicts"]] == ["QLTY", "DSTR"]
    assert main(["--demo", "--personas", "warren_buffett"]) == 0
    assert "consensus" in capsys.readouterr().out
