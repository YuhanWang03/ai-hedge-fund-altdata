"""Telegram card formatting for money-flow divergence signals.

Numbers come from the detector (Python-computed); the 多头/空头 lines come
from the narrator (LLM, no numbers). A fixed disclaimer footer makes the
proxy nature explicit on every card.
"""

from __future__ import annotations

import html

from v2.moneyflow.models import MoneyFlowSignal

_KIND_ZH = {"accumulation": "疑似吸筹", "distribution": "疑似派发/出货"}
_STRENGTH_ZH = {"strong": "强信号", "moderate": "中等强度"}
_PRICE_ZH = {"up": "上涨", "flat": "横盘", "down": "下跌"}
_FLOW_ZH = {"inflow": "净流入", "outflow": "净流出", "neutral": "中性"}
_ZONE_ZH = {
    "oversold": "超卖区", "low": "低位区", "neutral": "中性区",
    "high": "高位区", "overbought": "超买区",
}
_DIVERGENCE_ZH = {"bullish": "底背离", "bearish": "顶背离"}


def format_signal_card(signal: MoneyFlowSignal, *, price_window: int = 20) -> str:
    """Render one divergence signal as an HTML Telegram card."""
    kind_zh = _KIND_ZH.get(signal.kind, signal.kind)
    strength_zh = _STRENGTH_ZH.get(signal.strength, signal.strength)

    # Only surface the RSI-divergence chip when it *confirms* the verdict —
    # a bullish divergence on a distribution card (or vice-versa) would read
    # as self-contradictory. Such opposing divergences never drive the
    # strength grade either (see detector), so hiding them here is honest.
    _confirming = {"accumulation": "bullish", "distribution": "bearish"}
    div_suffix = ""
    if signal.rsi_divergence == _confirming.get(signal.kind):
        div_suffix = f" · {_DIVERGENCE_ZH[signal.rsi_divergence]}"

    lines = [
        f"📊 <b>资金流背离 · {html.escape(signal.ticker)}</b>",
        f"{kind_zh} · {strength_zh}",
        "",
        f"价格：近 {price_window} 日{_PRICE_ZH.get(signal.price_state, signal.price_state)}"
        f" ({signal.price_return:+.1%})",
        f"资金流：CMF {signal.cmf:+.2f}（{_FLOW_ZH.get(signal.flow_state, signal.flow_state)}）",
        f"动量：RSI {signal.rsi:.0f}（{_ZONE_ZH.get(signal.rsi_zone, signal.rsi_zone)}{div_suffix}）",
    ]

    if signal.bull:
        lines += ["", f"💡 多头视角：{html.escape(signal.bull)}"]
    if signal.bear:
        lines.append(f"🔻 空头视角：{html.escape(signal.bear)}")

    lines += [
        "",
        "⚠️ 量价代理指标（CMF/RSI）推断，非逐笔主力资金；背离信号存在假信号，"
        "请结合基本面与机构持仓交叉验证。",
    ]
    return "\n".join(lines)
