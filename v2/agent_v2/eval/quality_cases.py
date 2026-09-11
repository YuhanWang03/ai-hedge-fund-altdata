"""Graded questions: what a good answer must say, what it must not, and where it must come from.

Each case is a real question with a rubric.  ``criteria`` are sentences a
model judge checks against the answer (met or not, with the sentence that
meets it); ``forbidden`` are assertions the answer must not make; the
deterministic checks are the route, the sub-agents expected to run and the
source kinds that must be cited.  The quality run scores every case and
keeps the record, so a change can be read as "pass rate went from X to Y"
instead of one person reading Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from v2.agent_v2.models import RouteKind


@dataclass(frozen=True)
class QualityCase:
    id: str
    question: str
    #: Sentences the answer must satisfy; the judge decides each one.
    criteria: tuple[str, ...]
    #: Assertions the answer must not make; the judge decides each one.
    forbidden: tuple[str, ...] = ()
    #: Source kinds (``EvidenceItem.source_id`` prefixes) at least one cited item must come from.
    must_cite: tuple[str, ...] = ()
    expected_route: RouteKind | None = None
    #: Sub-agent names that must have run (``move_attributor``, ``news_checker``, ``filing_reader``, ``debater``).
    expected_agents: tuple[str, ...] = ()
    allow_web: bool = True
    #: Where the case came from: ``seed`` or ``feedback`` (a user's correction turned into a case).
    origin: str = "seed"
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


QUALITY_CASES: tuple[QualityCase, ...] = (
    QualityCase(
        "q_today_attribution",
        "AAPL今天为什么涨？",
        criteria=(
            "给出了当日涨跌幅，并说明是盘中还是收盘口径",
            "给出了行业基准（如 XLK）同日回报和相对表现的对照",
            "把没有直接证据的解释标为候选或可能相关，而不是已确认原因",
            "说明了接下来值得观察什么",
        ),
        forbidden=("把候选解释说成已确认的原因", "声称申报的正文没有被读取"),
        must_cite=("market_data",),
        expected_route=RouteKind.RESEARCH,
        expected_agents=("move_attributor",),
        tags=("attribution", "today"),
    ),
    QualityCase(
        "q_english_attribution",
        "why did AMD drop today",
        criteria=("用中文回答", "给出了当日涨跌幅和口径", "给出了行业基准对照"),
        forbidden=("把候选解释说成已确认的原因",),
        must_cite=("market_data",),
        expected_route=RouteKind.RESEARCH,
        expected_agents=("move_attributor",),
        tags=("attribution", "english"),
    ),
    QualityCase(
        "q_drawdown_chain",
        "ARM买入以来跌了这么多，是什么原因？",
        criteria=(
            "先说明买入以来的浮亏和这段跌幅落在哪个区间，再说单日涨跌只是旁注",
            "给出了从高点到低点的回撤幅度和同期行业基准的对照",
            "按日期列出了跌幅最大的交易日，并对每一天分别说有没有确认的原因",
            "把已读取的申报事件与对应日期对应起来，或明说该日没有对应事件",
        ),
        forbidden=("声称申报的正文或内容没有被读取", "用今天的涨跌解释买入以来的亏损"),
        must_cite=("market_data",),
        expected_agents=("move_attributor", "filing_reader"),
        tags=("drawdown",),
    ),
    QualityCase(
        "q_news",
        "ARM最近有哪些新闻？",
        criteria=(
            "按日期列出了近两周可核实的事件，每条有来源",
            "分别交代了网页新闻、SEC 申报和盯盘记录三类来源各有什么或没有什么",
            "指出了主要风险和数据缺口",
        ),
        forbidden=("把没有日期或来源的传闻当作事件",),
        must_cite=("web",),
        expected_agents=("news_checker",),
        tags=("news",),
    ),
    QualityCase(
        "q_news_paraphrase",
        "英特尔最近有什么动静？",
        criteria=("把英特尔识别为 INTC 并围绕它回答", "分别交代了网页新闻、SEC 申报和盯盘记录三类来源的结果"),
        expected_agents=("news_checker",),
        tags=("news", "paraphrase"),
    ),
    QualityCase(
        "q_valuation",
        "分析NVDA估值",
        criteria=(
            "给出了滚动市盈率等估值倍数并引用来源",
            "把估值和增长、盈利能力放在一起判断，而不是只报倍数",
            "明确指出前瞻口径缺失或其他数据缺口",
            "对异常高的比率（如 ROIC 接近 100%）提示口径依赖",
        ),
        forbidden=("把历史数据写成未来收益保证",),
        must_cite=("fd_", "sec_"),
        expected_route=RouteKind.RESEARCH,
        expected_agents=("debater",),
        tags=("research", "valuation"),
    ),
    QualityCase(
        "q_compare",
        "MU和SNDK哪个更值得购买？",
        criteria=(
            "对两只股票用同口径的数字比较（增长、估值、盈利兑现）",
            "明确说现有证据不足以无条件判定谁更值得买，或给出有条件的结论",
            "指出两边的数据缺口",
        ),
        forbidden=("给出无条件的买入建议",),
        expected_route=RouteKind.RESEARCH,
        tags=("compare",),
    ),
    QualityCase(
        "q_watchlist_volume",
        "我关注的股票里有没有最近在放量的？",
        criteria=("对关注列表里的每只股票给出成交量相对均量的倍数", "说明盘中口径不能据此判定放量或缩量（如果是盘中）", "指出哪几只相对靠前"),
        forbidden=("说没有成交量数据",),
        must_cite=("market_data",),
        tags=("watchlist", "performance"),
    ),
    QualityCase(
        "q_portfolio_ranking",
        "我的持仓里哪只跌的最惨？",
        criteria=("点名跌得最多的那只并给出浮亏百分比", "提到紧随其后的一两只", "指出组合层面的风险（如集中度）"),
        must_cite=("account.portfolio",),
        tags=("portfolio", "ranking"),
    ),
    QualityCase(
        "q_earnings_calendar",
        "接下来两周我的持仓里谁要出财报？",
        criteria=("直接回答未来两周持仓里有没有财报安排", "如果没有，说明这是日历口径，不等于确定没有"),
        tags=("portfolio", "earnings"),
    ),
    QualityCase(
        "q_knowledge",
        "市盈率和市销率有什么区别？",
        criteria=("给出两者的定义和分母的差别", "说明各自适用的场景和局限", "声明这是通用知识，未使用实时数据"),
        forbidden=("引用某只具体股票当前的市盈率数字",),
        expected_route=RouteKind.GENERAL_KNOWLEDGE,
        allow_web=False,
        tags=("knowledge",),
    ),
    QualityCase(
        "q_briefing",
        "美股今天有啥注意的？",
        criteria=("点出当天或近几天的宏观数据和事件（如 CPI、FOMC）", "给出组合的当日盈亏、集中度或回撤", "说明未来两周有没有财报安排"),
        forbidden=("逐只解释持仓当天的涨跌原因",),
        tags=("briefing",),
    ),
    QualityCase(
        "q_command",
        "NVDA涨到240时提醒我",
        criteria=("说明将要执行的写操作并等待确认，没有直接执行",),
        expected_route=RouteKind.COMMAND,
        allow_web=False,
        tags=("command",),
    ),
)


def from_feedback(rows: list[dict[str, Any]], *, limit: int = 20) -> tuple[QualityCase, ...]:
    """Turn negative feedback into cases: the question, with the user's correction as the criterion."""

    cases: list[QualityCase] = []
    seen: set[str] = set()
    for row in reversed(rows):
        if str(row.get("verdict") or "") != "bad":
            continue
        question = " ".join(str(row.get("question") or "").split())
        if not question or question in seen:
            continue
        seen.add(question)
        note = " ".join(str(row.get("note") or "").split())
        criterion = f"回答不再出现用户指出的问题：{note}" if note else "回答与用户上次指出有误的回答不同，并且有证据支持"
        cases.append(QualityCase(f"fb_{len(cases) + 1}", question, criteria=(criterion,), origin="feedback", note=note, tags=("feedback",)))
        if len(cases) >= limit:
            break
    return tuple(cases)
