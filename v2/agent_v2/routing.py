"""Low-cost request normalization and deterministic first-stage routing."""

from __future__ import annotations

import re

from v2.agent_v2.entities import extract_entities
from v2.agent_v2.models import NormalizedRequest, RouteDecision, RouteKind

# A mutation needs both a verb and a user-state object: bare English verbs
# such as ``add`` used to match inside ``addressable`` and ``padded``.
_COMMAND = re.compile(
    r"添加|移除|删除|加入.{0,12}(?:关注|自选|提醒|列表)"
    r"|设置.{0,8}提醒|取消.{0,8}提醒|提醒我|(?:涨到|跌到|涨破|跌破|高于|低于|突破).{0,12}(?:提醒|通知)"
    r"|\b(?:add|remove|delete)\b.{0,40}\b(?:watchlist|alerts?)\b"
    r"|\b(?:watchlist|alerts?)\b.{0,40}\b(?:add|remove|delete)\b"
    r"|\b(?:set|create|cancel)\s+(?:an?\s+|the\s+)?alerts?\b",
    re.I,
)
_LAB = re.compile(r"筛选|选股|回测|参数扫描|事件研究|异常收益|委员会|大师投票|screen|backtest|event study", re.I)
_DEEP = re.compile(r"完整报告|深度研究|全部模块|批量研究|全量研究", re.I)
_RESEARCH = re.compile(
    r"研究|分析|投资逻辑|牛熊|证伪|风险|比较|对比|谁更|哪[只个].{0,8}最|为什么|变化",
    re.I,
)
_KNOWLEDGE = re.compile(r"^(什么是|如何理解|怎么理解|解释一下|区别|原理|定义|为什么通常)", re.I)


def normalize_request(
    text: str,
    *,
    session_id: str = "",
    allow_web: bool = False,
    metadata: dict | None = None,
) -> NormalizedRequest:
    original = text or ""
    cleaned = original.strip()
    forced = False
    if cleaned.lower().startswith("/ask-v2"):
        remainder = cleaned[len("/ask-v2") :].lstrip(" \t:：,，")
        if remainder:
            cleaned, forced = remainder, True
    return NormalizedRequest(
        original_text=original,
        text=cleaned,
        session_id=session_id,
        entities=extract_entities(cleaned),
        forced_agent=forced,
        allow_web=allow_web,
        metadata=dict(metadata or {}),
    )


def route(request: NormalizedRequest) -> RouteDecision:
    text = request.text
    if _COMMAND.search(text):
        return RouteDecision(RouteKind.COMMAND, ("command",), "explicit user-state mutation")
    if _LAB.search(text):
        asynchronous = bool(_DEEP.search(text) or re.search(r"标普|全市场|全部股票|十年", text))
        return RouteDecision(
            RouteKind.ASYNC if asynchronous else RouteKind.LAB,
            ("lab",),
            "quantitative experiment requested",
            asynchronous=asynchronous,
        )
    if request.forced_agent:
        return RouteDecision(RouteKind.RESEARCH, ("research", "account"), "forced by /ask-v2")
    if _KNOWLEDGE.search(text) and not request.entities:
        return RouteDecision(RouteKind.GENERAL_KNOWLEDGE, (), "stable conceptual question")
    if _RESEARCH.search(text):
        packs = ("research", "account") if re.search(r"持仓|仓位|仓库|组合|账户", text) else ("research",)
        return RouteDecision(RouteKind.RESEARCH, packs, "multi-source research or synthesis")
    return RouteDecision(RouteKind.FAST_LOOKUP, ("account", "research"), "single-purpose lookup")
