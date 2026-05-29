"""Natural-language intent classifier (Stage 3).

User sends free-form text → DeepSeek (temperature=0) maps to a closed enum
of intents → bot routes to the same responders as the slash commands.

Critical design constraint: the LLM only DECIDES which tool to call. It
does not generate answers. Outputs that don't parse to a valid enum value
become "unknown" — guaranteed bounded behavior.

If the user wanted DeepSeek to write financial analysis from imagination,
they could ask DeepSeek directly. The point of this bot is grounded,
multi-source, verified output.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek

logger = logging.getLogger(__name__)

IntentName = Literal[
    "explain_move",
    "summary",
    "chain",
    "thirteen_f",
    "holders_view",
    "etf_view",
    "watchlist_view",
    "watchlist_add",
    "watchlist_remove",
    "settings",
    "find_anomalies",
    "alert_set",
    "alert_list",
    "portfolio_view",
    "pnl_view",
    "unknown",
]

_VALID_INTENTS = {
    "explain_move",
    "summary",
    "chain",
    "thirteen_f",
    "holders_view",
    "etf_view",
    "watchlist_view",
    "watchlist_add",
    "watchlist_remove",
    "settings",
    "find_anomalies",
    "alert_set",
    "alert_list",
    "portfolio_view",
    "pnl_view",
    "unknown",
}


_SYSTEM_PROMPT = (
    "你是一个股票分析助手的【意图分类器】。\n"
    "把用户的话归类到下列**固定 15 个 intent 之一**，并提取参数。"
    "你不要回答问题，只做分类。\n"
    "\n"
    "【支持的 intents】\n"
    "- explain_move: 用户想知道某只股票最近为什么涨/跌/异动\n"
    "- summary: 用户想看某只股票的综合概览（价格 + 财务 + 新闻）\n"
    "- chain: 用户想找某只股票的产业链/上下游/同业相关股\n"
    "- thirteen_f: 用户问某个机构 manager 最新持仓（巴菲特/Burry/Ark/Pershing/Citadel 等）\n"
    "- holders_view: 用户问某只股票被哪些机构持有 / 谁持仓 / 哪些大佬买了\n"
    "- etf_view: 用户问 ARK 系列 ETF（ARKK/ARKQ/ARKG/ARKW/ARKF）的最新每日持仓\n"
    "- watchlist_view: 用户想看自己的关注列表\n"
    "- watchlist_add: 用户想添加股票到 watchlist\n"
    "- watchlist_remove: 用户想从 watchlist 移除股票\n"
    "- settings: 用户想看推送阈值设置\n"
    "- find_anomalies: 用户想了解最近市场异动情况\n"
    "- alert_set: 用户想设置价格提醒 / 突破提醒 / 跌破提醒（含 ticker + 价格 + 方向）\n"
    "- alert_list: 用户想查看已设的价格提醒\n"
    "- portfolio_view: 用户想看自己的 Alpaca 账户当前持仓\n"
    "- pnl_view: 用户想看自己的账户盈亏 / 资产总额 / 当日 P&L\n"
    "- unknown: 都不匹配 / 含糊不清 / 与股票无关\n"
    "\n"
    "【参数提取规则】\n"
    "- ticker: 美股 ticker（1-5 大写字母）。如果用户用公司名，映射到 ticker"
    "（如 苹果→AAPL、英伟达→NVDA、微软→MSFT、谷歌→GOOGL、特斯拉→TSLA、"
    "Meta→META、亚马逊→AMZN、AMD→AMD、Palantir→PLTR、Snowflake→SNOW 等）\n"
    "- manager: 13F manager 别名，用小写。"
    "支持: brk/berkshire/buffett, burry/scion, ackman/pershing, einhorn/greenlight, "
    "renaissance/rentech, twosigma, deshaw/shaw, citadel, coatue, ark/cathie/wood\n"
    "- etf: 当 intent=etf_view 时，提取 ETF 符号（仅大写）。"
    "支持 ARKK / ARKG / ARKW / ARKF（ARKQ 暂不可用）。"
    "用户说『ARK 创新基金』→ ARKK，『ARK 基因组』→ ARKG，"
    "『Cathie 今天买啥』默认 → ARKK。\n"
    "- target_price: 当 intent=alert_set 时，提取目标价（数字，不带美元符号）\n"
    "- direction: 当 intent=alert_set 时，提取方向（仅 'above' 或 'below'）。"
    "「突破」「涨到」「站上」→ above；「跌破」「跌到」「跌穿」→ below\n"
    "\n"
    "【约束】\n"
    "1. **只输出 JSON，不要 markdown，不要解释**\n"
    "2. 不确定时输出 unknown\n"
    "3. ticker / manager / etf / direction 字段没有时输出空字符串\n"
    "4. target_price 没有时输出 0\n"
    "\n"
    "【JSON 格式】\n"
    '{"intent": "explain_move", "ticker": "NVDA", "manager": "", "etf": "", '
    '"target_price": 0, "direction": "", "raw": "..."}'
)


def classify(text: str) -> dict:
    """Map a free-form user message to a fixed intent + extracted params.

    Returns a dict with these keys:
        intent  — one of the 9 valid intent strings (defaults to "unknown")
        ticker  — uppercase US ticker, or "" if not present
        manager — lowercase manager alias, or "" if not present
        raw     — the LLM's own short echo of the user's intent (debug)
    """
    try:
        llm = ChatDeepSeek(model="deepseek-chat", temperature=0.0)
        response = llm.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=text),
        ])
    except Exception as exc:
        logger.warning("Intent classifier LLM failed: %s", exc)
        return _unknown(text)

    content = (response.content or "").strip()
    # Strip ```json fences if present
    if content.startswith("```"):
        lines = content.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return _unknown(text)
    except json.JSONDecodeError as exc:
        logger.warning("Intent JSON parse failed (%r): %s", content[:120], exc)
        return _unknown(text)

    intent = str(parsed.get("intent", "")).strip()
    if intent not in _VALID_INTENTS:
        intent = "unknown"

    # Defensive numeric parsing — model sometimes returns strings
    try:
        target_price = float(parsed.get("target_price", 0) or 0)
    except (TypeError, ValueError):
        target_price = 0.0

    return {
        "intent": intent,
        "ticker": str(parsed.get("ticker", "")).strip().upper(),
        "manager": str(parsed.get("manager", "")).strip().lower(),
        "etf": str(parsed.get("etf", "")).strip().upper(),
        "target_price": target_price,
        "direction": str(parsed.get("direction", "")).strip().lower(),
        "raw": str(parsed.get("raw", text))[:80],
    }


def _unknown(text: str) -> dict:
    return {
        "intent": "unknown",
        "ticker": "",
        "manager": "",
        "etf": "",
        "target_price": 0.0,
        "direction": "",
        "raw": text[:80],
    }
