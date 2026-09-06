"""Tests for routing, session state and the bot bridge.

The router decides how much a query is allowed to cost, so its failure modes are
asymmetric: sending a one-hop question to the agent wastes 10x, while sending a
multi-hop question to the fast path returns a confidently wrong-shaped answer.
Both directions are asserted here, along with the cases that look like signals
but must not fire — 最近 / 最新 are time words, and an enumerator intent already
returns the whole set.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent import bot_bridge, router, session  # noqa: E402
from v2.agent.fixtures import build_registry  # noqa: E402
from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall  # noqa: E402
from v2.agent.samples import CASES, PRONOUN_CASES  # noqa: E402


class _Env:
    """Set env vars for a block and restore them, including 'was unset'."""

    def __init__(self, **values: str) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        return False


def _parsed(intent: str, ticker: str = "") -> dict:
    return {"intent": intent, "ticker": ticker}


# ---------------------------------------------------------------------------
# router — modes
# ---------------------------------------------------------------------------

def test_off_mode_reproduces_current_behaviour():
    for case in CASES:
        decision = router.route(case.query, _parsed(case.intent, case.ticker), mode="off")
        if case.query.startswith("/ask"):
            assert decision.path == "agent", "显式转义在任何 mode 下都生效"
        elif case.query.startswith("/"):
            assert decision.path == "slash"
        else:
            assert decision.path == "single_hop", case.query


def test_unrecognised_flag_value_degrades_to_off():
    with _Env(V2_AGENT_ROUTING="heuristics"):     # typo: trailing s
        assert router.routing_mode() == "off"
    with _Env(V2_AGENT_ROUTING="HEURISTIC"):
        assert router.routing_mode() == "heuristic"


def test_unknown_only_routes_just_the_dead_end():
    assert router.route("今天有什么值得注意的", _parsed("unknown"),
                        mode="unknown_only").path == "agent"
    assert router.route("我持仓里哪只最危险", _parsed("risk_view"),
                        mode="unknown_only").path == "single_hop"


def test_every_labelled_case_routes_as_expected():
    """The whole sample set, in heuristic mode — the CI gate on the signal table."""
    misses = []
    for case in CASES:
        decision = router.route(case.query, _parsed(case.intent, case.ticker),
                                mode="heuristic")
        if decision.path != case.expected:
            misses.append(f"{case.query}: 期望 {case.expected} 实际 {decision.path}")
    assert not misses, "路由错误：\n" + "\n".join(misses)


def test_the_eval_set_routes_with_two_known_misses():
    """Routing accuracy on the 83-case eval set, pinned to its known limits.

    Both misses come from the same blind spot, in opposite directions: what the
    earnings-calendar card happens to cover is invisible from the query.

    * m11 (「我持仓里的半导体股票下次财报都是什么时候」) needs escalating,
      because the card's 14-day horizon cannot reach NVDA's November date.
    * m12 (「关注列表里有没有快发财报的」) does not, because the card annotates
      watchlist members and answers it outright.

    Nothing in either wording distinguishes them, so the router cannot decide
    both correctly a priori. Pinning the pair keeps the 99% claim honest and
    turns any *new* miss into a test failure.
    """
    from v2.agent.eval.cases import CASES as EVAL_CASES

    known = {"m11", "m12"}
    misses = {
        case.id for case in EVAL_CASES
        if router.route(case.query, _parsed(case.intent, case.ticker),
                        mode="heuristic").path != case.expected_path
    }
    assert misses == known, f"路由偏差变了：新增 {misses - known}，修复 {known - misses}"


# ---------------------------------------------------------------------------
# router — individual signals
# ---------------------------------------------------------------------------

def test_time_words_are_not_superlatives():
    """最近 / 最新 / 最后 all contain 最 and none of them mean 'most'."""
    for query in ("AMD 最近有什么 8-K", "NVDA 最新消息", "最近 CPI", "最后一次财报"):
        decision = router.route(query, _parsed("summary", "AMD"), mode="heuristic")
        assert decision.path == "single_hop", query


def test_enumerator_intents_are_exempt_from_the_collection_signal():
    assert router.route("我关注了哪些股票", _parsed("watchlist_view"),
                        mode="heuristic").path == "single_hop"
    # …but ranking inside that set still needs per-item lookups
    decision = router.route("watchlist 里哪只最值得关注", _parsed("watchlist_view"),
                            mode="heuristic")
    assert decision.path == "agent" and decision.signal == "superlative"


def test_named_subject_keeps_a_collection_query_on_the_fast_path():
    decision = router.route("CRWD 在我持仓里表现如何", _parsed("summary", "CRWD"),
                            mode="heuristic")
    assert decision.path == "single_hop"


def test_multi_ticker_and_acronym_stoplist():
    assert router.route("NVDA 和 AMD 谁强", _parsed("summary", "NVDA"),
                        mode="heuristic").path == "agent"
    # CEO / SEC / ETF are not tickers — the multi_ticker signal must not fire.
    # (The query does raise two topics, so it still escalates; the assertion is
    # about which signal fired, not about the path.)
    decision = router.route("SEC 对 CEO 的 ETF 规定", _parsed("summary", ""),
                            mode="heuristic")
    assert decision.signal != "multi_ticker"


def test_causal_needs_to_be_about_the_book_and_outside_its_own_card():
    """A causal question is only expensive when its own card cannot answer it.

    "我这周为什么亏钱" reads like multi-hop attribution, but the weekly P&L card
    already prints per-position contributions — the evaluation set proved the
    fast path answers it. So the signal stands down when the classifier landed
    on a card that already aggregates the book.
    """
    assert router.route("NVDA 为什么涨", _parsed("explain_move", "NVDA"),
                        mode="heuristic").path == "single_hop"
    assert router.route("我这周为什么亏钱", _parsed("pnl_period"),
                        mode="heuristic").path == "single_hop"
    assert router.route("为什么我的半导体仓位表现分化这么大", _parsed("unknown"),
                        mode="heuristic").path == "agent"


def test_decision_records_which_signal_fired():
    decision = router.route("我持仓里哪只最危险", _parsed("risk_view"), mode="heuristic")
    assert decision.signal == "superlative"
    assert decision.reason and decision.mode == "heuristic"
    assert "多步" in router.explain(decision)


def test_a_broken_predicate_cannot_break_routing():
    def explodes(text, parsed):
        raise RuntimeError("boom")

    original = router.HEURISTIC_SIGNALS
    router.HEURISTIC_SIGNALS = ((("boom"), explodes, "x"),) + original
    try:
        decision = router.route("我持仓里哪只最危险", _parsed("risk_view"), mode="heuristic")
        assert decision.path == "agent"          # the surviving signals still fire
    finally:
        router.HEURISTIC_SIGNALS = original


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

def test_pronoun_resolution_cases():
    store = session.SessionStore()
    for chat_id, (first, follow_up, expected) in enumerate(PRONOUN_CASES):
        store.record(chat_id, session.Turn(
            query=first, tickers=tuple(session.extract_tickers(first))))
        resolution = store.resolve(chat_id, follow_up)
        actual = resolution.antecedent if resolution.rewritten else ""
        assert actual == expected, f"{first} → {follow_up}"


def test_leading_demonstrative_is_absorbed():
    store = session.SessionStore()
    store.record(1, session.Turn(query="NVDA 怎么样", tickers=("NVDA",)))
    assert store.resolve(1, "那它财报呢").text == "NVDA财报呢"


def test_expired_turns_stop_being_antecedents():
    store = session.SessionStore(ttl_seconds=0.05)
    store.record(1, session.Turn(query="NVDA 怎么样", tickers=("NVDA",)))
    assert store.resolve(1, "它财报呢").rewritten
    time.sleep(0.08)
    assert not store.resolve(1, "它财报呢").rewritten


def test_history_is_bounded():
    store = session.SessionStore(max_turns=3)
    for i in range(10):
        store.record(1, session.Turn(query=f"q{i}"))
    assert len(store.recent(1, n=99)) == 3


def test_sessions_do_not_leak_across_chats():
    store = session.SessionStore()
    store.record(1, session.Turn(query="NVDA 怎么样", tickers=("NVDA",)))
    assert not store.resolve(2, "它财报呢").rewritten


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------

def test_enabled_is_false_by_default():
    with _Env(V2_AGENT_ROUTING="off"):
        assert bot_bridge.enabled() is False
    with _Env(V2_AGENT_ROUTING="unknown_only"):
        assert bot_bridge.enabled() is True


def test_bridge_takes_the_fast_path_without_touching_the_llm():
    llm = ScriptedLLM([])          # any call would exhaust it and show up
    result = bot_bridge.handle_nl_sync(
        "组合风险怎么样", chat_id=1, parsed=_parsed("risk_view"),
        registry=build_registry(), llm=llm, store=session.SessionStore(),
        mode="heuristic")

    assert result.path == "single_hop"
    assert result.baseline is not None and result.agent is None
    assert llm.calls == [], "单跳路径不该调用 agent 的 LLM"
    assert result.stats()["tool_calls"] == 1


def test_bridge_runs_the_agent_on_a_multi_hop_query():
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")],
                    prompt_tokens=100, completion_tokens=10),
        LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓。",
                    prompt_tokens=200, completion_tokens=20),
    ])
    result = bot_bridge.handle_nl_sync(
        "我持仓里哪只最危险", chat_id=1, parsed=_parsed("risk_view"),
        registry=build_registry(), llm=llm, store=session.SessionStore(),
        mode="heuristic")

    assert result.path == "agent" and result.decision.signal == "superlative"
    assert result.agent is not None and result.baseline is None
    assert "CRWD" in result.answer
    assert result.stats()["path"] == "agent"


def test_bridge_never_reclassifies_when_given_parsed():
    calls: list[str] = []

    def classifier(text):
        calls.append(text)
        return _parsed("risk_view")

    bot_bridge.handle_nl_sync("组合风险", chat_id=1, parsed=_parsed("risk_view"),
                              classifier=classifier, registry=build_registry(),
                              store=session.SessionStore(), mode="heuristic")
    assert calls == [], "bot 已经分类过一次，桥接层不该再付一次钱"


def test_bridge_streams_progress_lines():
    seen: list[str] = []
    llm = ScriptedLLM([
        LLMResponse(text="先看持仓",
                    tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
        LLMResponse(text="CRWD 占仓 22.4%。"),
    ])
    result = bot_bridge.handle_nl_sync(
        "我持仓里哪只最危险", chat_id=1, parsed=_parsed("risk_view"),
        registry=build_registry(), llm=llm, store=session.SessionStore(),
        on_progress=seen.append, mode="heuristic")

    assert seen, "应当有进度推送"
    assert "portfolio_view" in seen[-1]
    assert result.progress_log and result.progress_log[-1].startswith("✅")


def test_bridge_resolves_pronouns_and_discloses_the_rewrite():
    store = session.SessionStore()
    store.record(1, session.Turn(query="NVDA 怎么样", tickers=("NVDA",)))

    result = bot_bridge.handle_nl_sync(
        "那它财报呢", chat_id=1, parsed=_parsed("earnings_view", "NVDA"),
        registry=build_registry(), store=store, mode="heuristic")

    assert result.resolved_query == "NVDA财报呢"
    assert result.path == "single_hop", "补全之后单跳就够了 — 这正是补全的价值"
    assert "补全为" in result.answer, "改写必须对用户可见"


def test_an_unverifiable_answer_is_labelled_as_such():
    """The loop knew it could not ground these figures; the user must know too."""
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
        LLMResponse(text="组合年化波动率 37.9%。"),      # invented
        LLMResponse(text="组合年化波动率 41.2%。"),      # repair also invented
    ])
    result = bot_bridge.handle_nl_sync(
        "我持仓里哪只最危险", chat_id=1, parsed=_parsed("risk_view"),
        registry=build_registry(), llm=llm, store=session.SessionStore(),
        mode="heuristic")

    assert result.agent.stop_reason == "final_answer_ungrounded"
    assert result.answer.startswith("⚠️")
    assert "41.2" in result.answer, "要点名是哪个数字站不住"


def test_a_grounded_answer_carries_no_warning():
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
        LLMResponse(text="CRWD 占仓 22.4%。"),
    ])
    result = bot_bridge.handle_nl_sync(
        "我持仓里哪只最危险", chat_id=1, parsed=_parsed("risk_view"),
        registry=build_registry(), llm=llm, store=session.SessionStore(),
        mode="heuristic")
    assert not result.answer.startswith("⚠️")


def test_bridge_survives_a_failing_classifier():
    def broken(text):
        raise RuntimeError("classifier down")

    result = bot_bridge.handle_nl_sync(
        "随便问点什么", chat_id=1, classifier=broken, registry=build_registry(),
        llm=ScriptedLLM([LLMResponse(text="分类器不可用，已直接回答。")]),
        store=session.SessionStore(), mode="unknown_only")

    assert result.parsed["intent"] == "unknown"
    assert result.path == "agent", "分类失败降级为 agent，好过「没听懂」"


def test_bridge_records_the_turn_for_the_next_question():
    store = session.SessionStore()
    bot_bridge.handle_nl_sync("NVDA 怎么样", chat_id=7, parsed=_parsed("summary", "NVDA"),
                              registry=build_registry(), store=store, mode="heuristic")
    assert store.last_ticker(7) == "NVDA"


# ---------------------------------------------------------------------------
# telegram_hook — the single call cmd_nl makes
# ---------------------------------------------------------------------------

class _FakePlaceholder:
    """Stands in for the Telegram message cmd_nl edits in place.

    ``limit`` mirrors Telegram's real 4096-character cap, which the bridge used
    to fail silently against; ``replies`` collects the follow-up messages a long
    answer is continued in.
    """

    def __init__(self, fail_html: bool = False,
                 limit: int = bot_bridge.TELEGRAM_LIMIT) -> None:
        self.edits: list[tuple[str, bool]] = []
        self.replies: list[str] = []
        self.fail_html = fail_html
        self.limit = limit

    async def edit_text(self, text, parse_mode=None, disable_web_page_preview=None):
        if self.fail_html and parse_mode == "HTML":
            raise RuntimeError("Bad Request: can't parse entities")
        if len(text) > self.limit:
            raise RuntimeError("Bad Request: message is too long")
        self.edits.append((text, parse_mode == "HTML"))

    async def reply_text(self, text, parse_mode=None, disable_web_page_preview=None):
        if len(text) > self.limit:
            raise RuntimeError("Bad Request: message is too long")
        self.replies.append(text)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_hook_hands_a_single_hop_query_back_to_the_existing_dispatch():
    """False means 'not mine' — and parsed comes back so nothing reclassifies."""
    placeholder = _FakePlaceholder()
    llm = ScriptedLLM([])
    handled, parsed, text = _run(bot_bridge.telegram_hook(
        "组合风险怎么样", 1, placeholder,
        classifier=lambda _t: _parsed("risk_view"),
        registry=build_registry(), llm=llm, store=session.SessionStore()))

    assert handled is False
    assert parsed["intent"] == "risk_view"
    assert text == "组合风险怎么样"
    assert placeholder.edits == [], "未接手就不该改动那条占位消息"
    assert llm.calls == []


def test_an_answer_longer_than_telegram_allows_still_arrives():
    """Seen live: a finished run left the placeholder saying 「分析中…」 for
    five minutes. editMessageText rejects anything over 4096 characters, and
    the except branch — whose own comment said "too long" — passed."""
    placeholder = _FakePlaceholder()
    long_answer = "\n\n".join(f"第 {i} 段：ARM 浮亏 -35.74%。" * 20 for i in range(30))
    assert len(long_answer) > bot_bridge.TELEGRAM_LIMIT

    _run(bot_bridge._deliver(placeholder, long_answer))

    assert placeholder.edits, "第一段必须写进占位消息"
    delivered = placeholder.edits[-1][0] + "".join(placeholder.replies)
    assert "第 0 段" in delivered and "第 29 段" in delivered, "整条回答都要送到"
    assert all(len(t) <= bot_bridge.TELEGRAM_LIMIT
               for t in [placeholder.edits[-1][0]] + placeholder.replies)


def test_a_short_answer_is_still_one_message():
    placeholder = _FakePlaceholder()
    _run(bot_bridge._deliver(placeholder, "CRWD 占仓 22.4%。"))
    assert placeholder.replies == [], "没超长就不该拆成两条"
    assert placeholder.edits[-1][0] == "CRWD 占仓 22.4%。"


def test_a_late_progress_line_cannot_overwrite_the_answer():
    """The other way a finished run looks hung: the last progress update is
    still in flight at Telegram when the answer is written, lands after it, and
    puts 「分析中…」 back on the screen for good."""
    placeholder = _FakePlaceholder()

    async def scenario():
        channel = bot_bridge.ProgressChannel(
            asyncio.get_running_loop(),
            lambda t: bot_bridge._edit(placeholder, t))
        channel("🤔 分析中…")
        channel("✅ 已生成回答")
        await channel.close()
        channel("✅ 迟到的进度")          # dropped: the channel is closed
        await bot_bridge._deliver(placeholder, "结论：ARM 最危险。")
        await asyncio.sleep(0)            # let anything still queued run
        return placeholder.edits[-1][0]

    assert _run(scenario()) == "结论：ARM 最危险。"
    assert "迟到" not in "".join(t for t, _ in placeholder.edits)


def test_the_hook_discloses_a_rewrite_it_performed_itself():
    """The hook resolves before routing, then calls handle_nl_sync with the
    resolved text. Re-resolving there finds nothing left to rewrite and reports
    rewritten=False, so the disclosure vanishes — which is what production did
    while the direct-call test kept passing."""
    store = session.SessionStore()
    store.record(1, session.Turn(query="我的持仓最近整体在跌还是涨？"))
    placeholder = _FakePlaceholder()
    llm = ScriptedLLM([
        LLMResponse(text="先看盈亏",
                    tool_calls=[ToolCall("c1", "pnl_view", {}, "{}")]),
        LLMResponse(text="本周 +0.13%。"),
        LLMResponse(text="本周 +0.13%。"),
    ])

    with _Env(V2_AGENT_ROUTING="unknown_only"):
        handled, _p, text = _run(bot_bridge.telegram_hook(
            "为什么？", 1, placeholder, classifier=lambda _t: _parsed("unknown"),
            registry=build_registry(), llm=llm, store=store))

    assert handled is True
    assert "我的持仓最近整体在跌还是涨" in text, "补全后的问题要交回调用方"
    assert placeholder.edits, "接手了就该改写占位消息"
    final = placeholder.edits[-1][0]
    assert "补全" in final, "改写了用户的问题就必须讲明"
    assert "本周 +0.13%" in final


def test_a_fixed_phrase_is_not_a_conjunction():
    """「还有多远」「还有几天」 are one question. Found while building the
    checker-stress cases: 「MSFT 离 52 周高点还有多远」 escalated on the compound
    signal. Third member of the family that already cost this router 最近／最新
    (superlative) and 这个月 (pronoun resolution)."""
    for query in ("MSFT 离 52 周高点还有多远", "还有几天发财报", "还有多少现金"):
        assert router.route(query, _parsed("moneyflow_view", "MSFT"),
                            mode="heuristic").signal != "compound", query

    # A real conjunction still fires.
    assert router.route("看一下 NVDA，另外 CPI 怎么样", _parsed("summary", "NVDA"),
                        mode="heuristic").signal == "compound"


def test_a_demonstrative_inside_a_time_word_is_not_a_pronoun():
    """「我这个月比上个月表现好还是差？」 came back as 「我NVDA月比上个月表现好
    还是差？」 — 这个 matched, the ticker went in, the question was destroyed and
    the agent answered about NVDA. Same shape as 最近／最新 in the router: a
    substring that looks like the thing but belongs to a time expression."""
    store = session.SessionStore()
    store.record(1, session.Turn(query="NVDA 为什么涨？", tickers=("NVDA",)))

    for query in ("我这个月比上个月表现好还是差？", "这个季度的回撤",
                  "这周怎么样", "那个月的盈亏"):
        assert store.resolve(1, query).text == query, query

    # …and a real demonstrative still resolves.
    assert store.resolve(1, "这只怎么样").text == "NVDA怎么样"


def test_a_write_never_escalates():
    """The agent runs with allow_mutations=False, so escalating a write means
    researching for ten seconds and then not doing the one thing that was asked.
    「把我持仓里跌超过 30% 的都加进关注列表」 hit the collection signal and did
    exactly that."""
    for query, intent in (("把我持仓里跌超过 30% 的都加进关注列表", "watchlist_add"),
                          ("给我持仓里最危险的那只设个提醒", "alert_set"),
                          ("把关注列表里没持仓的都删了", "watchlist_remove")):
        decision = router.route(query, _parsed(intent), mode="heuristic")
        assert decision.path == "single_hop", f"{query} 不该升级"

    # Derived from the registry, so a new mutating tool cannot slip past.
    from v2.agent.registry import ToolRegistry
    assert router.WRITE_INTENTS == frozenset(
        spec.name for spec in ToolRegistry().specs if spec.mutating)
    assert router.WRITE_INTENTS, "写意图集合为空说明取的地方错了"


def test_a_bare_follow_up_restores_the_previous_question():
    """"为什么？" on its own, seen live: no pronoun to substitute, so the
    resolver passed it through, the classifier returned unknown, and the agent
    spent a full budget concluding it should ask what the user meant."""
    store = session.SessionStore()
    store.record(1, session.Turn(query="我的持仓最近整体在跌还是涨？"))

    resolution = store.resolve(1, "为什么？")
    assert resolution.rewritten
    assert "我的持仓最近整体在跌还是涨" in resolution.text
    assert resolution.text.endswith("为什么？")
    assert "补全" in resolution.note, "改写过就要对用户讲明"

    # A question that carries its own subject is not a follow-up.
    assert store.resolve(1, "为什么 NVDA 涨").rewritten is False
    # Nothing to restore, and two bare follow-ups in a row restore nothing.
    assert session.SessionStore().resolve(9, "为什么？").rewritten is False
    store.record(2, session.Turn(query="为什么？"))
    assert store.resolve(2, "为什么？").rewritten is False


def test_ask_prefix_is_stripped_before_anything_sees_it():
    """The prefix must not reach the classifier or the agent as part of the
    question, and a bare /ask forces nothing — there is no query to route."""
    assert router.strip_ask_prefix("/ask NVDA 和 SMCI 谁更危险") == (
        "NVDA 和 SMCI 谁更危险", True)
    assert router.strip_ask_prefix("/ask：我持仓里哪只最危险") == (
        "我持仓里哪只最危险", True)
    assert router.strip_ask_prefix("NVDA 为什么涨") == ("NVDA 为什么涨", False)
    assert router.strip_ask_prefix("/ask") == ("/ask", False)
    assert router.route("/ask", mode="heuristic").path == "slash"


def test_hook_honours_ask_on_a_query_the_router_would_not_take():
    """/ask overrides the mode. "NVDA 为什么涨" is a textbook fast-path query,
    and under unknown_only nothing at all routes — the prefix still wins, and
    the agent receives the question without it."""
    placeholder = _FakePlaceholder()
    llm = ScriptedLLM([
        LLMResponse(text="先看异动",
                    tool_calls=[ToolCall("c1", "explain_move", {"ticker": "NVDA"}, "{}")]),
        LLMResponse(text="NVDA 今日 +0.90%。"),
    ])
    seen: list[str] = []

    def classifier(text):
        seen.append(text)
        return _parsed("explain_move", ticker="NVDA")

    with _Env(V2_AGENT_ROUTING="unknown_only"):
        handled, _parsed_out, text = _run(bot_bridge.telegram_hook(
            "/ask NVDA 为什么涨", 1, placeholder, classifier=classifier,
            registry=build_registry(), llm=llm, store=session.SessionStore()))

    assert handled is True
    assert seen == ["NVDA 为什么涨"], "分类器不该看到斜杠命令"
    assert text == "NVDA 为什么涨"
    assert placeholder.edits, "接手了就该改写占位消息"


def test_hook_answers_a_multi_hop_query_and_edits_the_placeholder():
    placeholder = _FakePlaceholder()
    llm = ScriptedLLM([
        LLMResponse(text="先看持仓",
                    tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
        LLMResponse(text="CRWD 占仓 22.4%，是第一大持仓。"),
    ])
    with _Env(V2_AGENT_ROUTING="heuristic"):
        handled, _parsed_out, _text = _run(bot_bridge.telegram_hook(
            "我持仓里哪只最危险", 1, placeholder,
            classifier=lambda _t: _parsed("risk_view"),
            registry=build_registry(), llm=llm, store=session.SessionStore()))

    assert handled is True
    assert placeholder.edits, "应当把答案写回占位消息"
    final = placeholder.edits[-1][0]
    assert "CRWD" in final and "多步分析" in final


def test_hook_resolves_a_pronoun_before_classifying():
    """The rewrite has to happen first — that is what lets the *fast* path answer."""
    store = session.SessionStore()
    store.record(1, session.Turn(query="NVDA 怎么样", tickers=("NVDA",)))
    seen: list[str] = []

    def classifier(text):
        seen.append(text)
        return _parsed("earnings_view", "NVDA")

    handled, _p, text = _run(bot_bridge.telegram_hook(
        "那它财报呢", 1, _FakePlaceholder(), classifier=classifier,
        registry=build_registry(), store=store))

    assert seen == ["NVDA财报呢"], "分类器看到的应当是补全后的问题"
    assert handled is False and text == "NVDA财报呢"


def test_hook_remembers_the_turn_for_the_next_question():
    store = session.SessionStore()
    _run(bot_bridge.telegram_hook(
        "NVDA 怎么样", 7, _FakePlaceholder(),
        classifier=lambda _t: _parsed("summary", "NVDA"),
        registry=build_registry(), store=store))
    assert store.last_ticker(7) == "NVDA", "快路径答完也要记住，否则下一轮指代不了"


def test_hook_falls_back_to_plain_text_when_html_fails():
    """A model-written answer can contain a stray '<'; losing it to a parse
    error is worse than losing the formatting."""
    placeholder = _FakePlaceholder(fail_html=True)
    llm = ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall("c1", "portfolio_view", {}, "{}")]),
        LLMResponse(text="CRWD 占仓 22.4% <未闭合"),
    ])
    with _Env(V2_AGENT_ROUTING="heuristic"):
        handled, _p, _t = _run(bot_bridge.telegram_hook(
            "我持仓里哪只最危险", 1, placeholder,
            classifier=lambda _t: _parsed("risk_view"),
            registry=build_registry(), llm=llm, store=session.SessionStore()))

    assert handled is True
    assert placeholder.edits and placeholder.edits[-1][1] is False, "应退回纯文本发送"
    assert "CRWD" in placeholder.edits[-1][0]


def test_hook_survives_a_dead_classifier():
    placeholder = _FakePlaceholder()
    llm = ScriptedLLM([LLMResponse(text="分类器不可用，已直接回答。")])

    def broken(_text):
        raise RuntimeError("classifier down")

    with _Env(V2_AGENT_ROUTING="unknown_only"):
        handled, parsed, _t = _run(bot_bridge.telegram_hook(
            "随便问点什么", 1, placeholder, classifier=broken,
            registry=build_registry(), llm=llm, store=session.SessionStore()))

    assert handled is True and parsed["intent"] == "unknown"


def test_production_budgets_are_tighter_than_the_harness_defaults():
    """A tool call is free in the eval and expensive live — explain_move alone
    spends a Tavily search and two internal LLM calls per invocation."""
    from v2.agent.loop import AgentConfig

    default = AgentConfig()
    live = bot_bridge.production_config()
    assert live.max_tool_calls < default.max_tool_calls
    assert live.max_steps <= default.max_steps


def test_production_budgets_are_tunable_without_a_deploy():
    with _Env(V2_AGENT_MAX_TOOL_CALLS="3", V2_AGENT_MAX_STEPS="2",
              V2_AGENT_MAX_SECONDS="30"):
        live = bot_bridge.production_config()
        assert (live.max_tool_calls, live.max_steps, live.max_seconds) == (3, 2, 30.0)
    with _Env(V2_AGENT_MAX_TOOL_CALLS="abc"):
        assert bot_bridge.production_config().max_tool_calls == 8, "写错的值降级为默认"


def test_progress_updates_are_throttled():
    sent: list[str] = []
    throttled = bot_bridge._Throttled(sent.append, interval=10.0)
    throttled("第一条")
    throttled("第二条")
    throttled("第三条")
    assert sent == ["第一条"], "Telegram 编辑有频率限制，密集进度必须丢弃"


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
