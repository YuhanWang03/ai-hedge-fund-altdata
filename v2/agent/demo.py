"""Zero-key demo scenario.

The comparison is only convincing if a reader can run it. With fixture tools the
run still needs an LLM key; scripting the model as well removes the last one, so
``python -m v2.agent.cli --demo`` works on a bare clone with no configuration at
all. The scripted trajectory is a real one — the same tool calls, failures and
grounding repair the loop produces against these fixtures — replayed instead of
regenerated.
"""

from __future__ import annotations

import json
from typing import Any

from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall

DEMO_QUERY = "我持仓里哪只最危险？"

# What the incumbent router does with this query: one label, one card.
DEMO_CLASSIFICATION: dict[str, Any] = {
    "intent": "risk_view", "ticker": "", "manager": "", "etf": "",
    "target_price": 0.0, "direction": "", "days_horizon": 0, "period": "",
    "days_back": 0, "release_type": "", "raw": "组合风险",
}


def _call(index: int, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call_{index}", name=name, arguments=arguments,
                    raw_arguments=json.dumps(arguments, ensure_ascii=False))


def _acts(text: str, *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls),
                       prompt_tokens=1400, completion_tokens=90)


def demo_llm() -> ScriptedLLM:
    """A five-turn trajectory that exercises every mechanism in the loop."""
    return ScriptedLLM([
        # 1. can't rank holdings without knowing them
        _acts("要判断哪只最危险，先得知道持仓和权重。", _call(1, "portfolio_view")),

        # 2. fan out on the facts that decide risk — chosen from step 1's result
        _acts("CRWD 22.4% 和 SMCI 8.6% 值得细看：先查组合层风险，再查这两只的财报、"
              "内部人和重大事项。",
              _call(2, "risk_view"),
              _call(3, "earnings_view", ticker="CRWD"),
              _call(4, "earnings_view", ticker="SMCI"),
              _call(5, "insider_view", ticker="CRWD"),
              _call(6, "eight_k_view", ticker="CRWD"),
              _call(7, "explain_move", ticker="CRWD"),
              _call(8, "eight_k_view", ticker="SMCI")),   # this one fails

        # 3. route around the failed tool
        _acts("SMCI 的 8-K 源超时，改用异动归因补上它的近期情况。",
              _call(9, "explain_move", ticker="SMCI")),

        # 4. an answer with one invented figure — trips the grounding check
        LLMResponse(
            text=("结论：<b>CRWD</b> 最危险。\n"
                  "· 占仓 22.4%，超过 20% 单一持仓阈值（portfolio_view / risk_view）\n"
                  "· 2026-09-06 盘后财报，距今 3 天；历史财报后次日平均波动 9.10%\n"
                  "· 90 天内 3 次内部人卖出、0 次买入，达到集群阈值（insider_view）\n"
                  "· 8-K 显示 CFO 将于 2026-10-01 离任（eight_k_view）\n"
                  "· 隐含波动率 78.4%，处于一年高位"),
            prompt_tokens=4200, completion_tokens=210, finish_reason="stop"),

        # 5. repaired: the unsupported figure is dropped, everything else stands
        LLMResponse(
            text=("结论：<b>CRWD</b> 最危险，其次是 SMCI。\n\n"
                  "<b>CRWD（占仓 22.4%）</b>\n"
                  "· 单一持仓 22.4% 超过 20% 阈值，前 3 大合计 54.7% 也超 50% 阈值\n"
                  "· 2026-09-06 盘后财报，距今 3 天；过去 4 次财报后次日平均绝对波动 9.10%\n"
                  "· 90 天内 3 次内部人卖出、0 次买入 — 达到集群卖出阈值\n"
                  "· 8-K：CFO 将于 2026-10-01 离任\n"
                  "· 今日 -2.10%，同期 XLK +0.60%，逆势 -2.70pp\n\n"
                  "<b>SMCI（占仓 8.6%）</b>\n"
                  "· 已浮亏 -21.5%，今日 -5.40%，相对 SMH 逆势 -8.30pp\n"
                  "· 2026-09-09 财报，上次 EPS miss -23.6%、财报后次日 -14.20%\n"
                  "· 权重不高，风险敞口小于 CRWD\n\n"
                  "数据缺口：SMCI 的 8-K 查询超时，重大事项未覆盖。"),
            prompt_tokens=4600, completion_tokens=320, finish_reason="stop"),
    ])


def demo_classifier(text: str) -> dict[str, Any]:
    return dict(DEMO_CLASSIFICATION)
