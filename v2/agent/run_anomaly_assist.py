"""PyCharm / CLI 入口：B1 异动补齐的端到端演示与打分，零 API key。

模型被脚本化（回放一条真实形状的轨迹），工具走录制观测，所以这个脚本
可以进 CI，每次改 B1 的约束都能立刻看到「无有效归因占比」怎么变。

三个被选中的标的刻意覆盖三种结局：
  ARM  → 补齐成功（8-K 里有能解释异动的合同）
  SMCI → 工具超时后如实报告「找不到」（诚实优于编造）
  PLTR → 产出了无法溯源的数字，整条被丢弃
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import copy  # noqa: E402
import json  # noqa: E402

from v2.agent.anomaly_assist import (  # noqa: E402
    AssistConfig, assist_batch, candidate_score, needs_assist, select_candidates,
)
from v2.agent.fixtures import build_anomaly_registry  # noqa: E402
from v2.agent.llm import LLMResponse, ScriptedLLM, ToolCall  # noqa: E402
from v2.agent.samples import ANOMALY_CASES, SimpleReason  # noqa: E402

_RULE = "─" * 78


def _call(index: int, name: str, ticker: str) -> ToolCall:
    return ToolCall(id=f"c{index}", name=name, arguments={"ticker": ticker},
                    raw_arguments=json.dumps({"ticker": ticker}))


def demo_llm() -> ScriptedLLM:
    """One scripted trajectory per selected ticker, in selection order."""
    return ScriptedLLM([
        # --- ARM: filings explain it ---------------------------------------
        LLMResponse(text="新闻没解释，先看公司自己的申报和资金流。",
                    tool_calls=[_call(1, "eight_k_view", "ARM"),
                                _call(2, "moneyflow_view", "ARM")],
                    prompt_tokens=900, completion_tokens=60),
        LLMResponse(text=json.dumps({"reasons": [{
            "text": "2026-09-03 提交 8-K Item 1.01：与一家超大规模云厂商签署 5 年、"
                    "总额 $2.40B 的架构授权协议，量级足以解释当日涨幅",
            "confidence": "高", "evidence_tool": "eight_k_view"}]},
            ensure_ascii=False),
            prompt_tokens=2100, completion_tokens=120),

        # --- SMCI: the 8-K source times out, and nothing else explains it ---
        LLMResponse(text="先查申报和财报。",
                    tool_calls=[_call(3, "eight_k_view", "SMCI"),
                                _call(4, "earnings_view", "SMCI")],
                    prompt_tokens=900, completion_tokens=55),
        LLMResponse(text=json.dumps({"reasons": []}, ensure_ascii=False),
                    prompt_tokens=1800, completion_tokens=30),

        # --- PLTR: produces a figure that traces to nothing -----------------
        LLMResponse(text="内部人交易值得看。",
                    tool_calls=[_call(5, "insider_view", "PLTR")],
                    prompt_tokens=850, completion_tokens=40),
        LLMResponse(text=json.dumps({"reasons": [{
            "text": "内部人连续减持，且机构持仓较上季度下降 31.5%，共同压制股价",
            "confidence": "中", "evidence_tool": "insider_view"}]},
            ensure_ascii=False),
            prompt_tokens=1700, completion_tokens=90),
    ])


def unexplained_rate(anomalies) -> tuple[int, int]:
    unexplained = sum(1 for a in anomalies if needs_assist(a))
    return unexplained, len(anomalies)


def main() -> int:
    anomalies = [copy.deepcopy(a) for a in ANOMALY_CASES]
    before, total = unexplained_rate(anomalies)

    print(f"{_RULE}\n【筛选】哪些异动值得花一次 agent 运行\n{_RULE}")
    print(f"  {'ticker':<8}{'涨跌':>9}{'量比':>7}{'逆势':>6}{'现有归因':>12}{'排序分':>9}  处置")
    selected = {a.ticker for a in select_candidates(anomalies)}
    for a in sorted(anomalies, key=candidate_score, reverse=True):
        state = ("无归因" if not a.reasons
                 else ("全部低置信" if all(r.confidence == "低" for r in a.reasons) else "已有高/中"))
        if a.ticker in selected:
            disposition = "→ 补齐"
        elif needs_assist(a):
            disposition = "超出每轮上限，原样推送"
        else:
            disposition = "跳过（已解释）"
        print(f"  {a.ticker:<8}{a.price_change_pct:>+8.2%}{a.volume_ratio:>7.1f}"
              f"{'✓' if a.contrarian else '·':>6}{state:>12}{candidate_score(a):>9.1f}  {disposition}")

    print(f"\n{_RULE}\n【补齐】每条的结局\n{_RULE}")
    outcomes = assist_batch(anomalies, llm=demo_llm(),
                            registry=build_anomaly_registry(),
                            config=AssistConfig(), factory=SimpleReason)
    for outcome in outcomes:
        mark = {"ok": "✅", "no_finding": "🟡", "ungrounded": "❌",
                "unparsable": "❌", "timeout": "⏱", "error": "❌"}.get(outcome.outcome, "·")
        print(f"  {mark} {outcome.ticker:<6} {outcome.outcome:<12}"
              f" 工具 {outcome.tool_calls} 次 {list(outcome.tools_used)}"
              f"  {outcome.tokens} token")
        if outcome.detail:
            print(f"       {outcome.detail}")
        for reason in outcome.reasons:
            print(f"       + [{reason.confidence}] {reason.text}")

    after, _ = unexplained_rate(anomalies)
    added = sum(len(o.reasons) for o in outcomes if o.ok)
    tokens = sum(o.tokens for o in outcomes)

    print(f"\n{_RULE}\n【效果】\n{_RULE}")
    print(f"  无有效归因占比   {before}/{total} = {before / total:.0%}"
          f"   →   {after}/{total} = {after / total:.0%}")
    print(f"  新增归因条数     {added}")
    print(f"  代价             {len(outcomes)} 次 agent 运行 · {tokens} token · "
          f"{sum(o.elapsed_ms for o in outcomes)} ms")
    print(f"  丢弃             "
          f"{sum(1 for o in outcomes if o.outcome == 'ungrounded')} 条未溯源，"
          f"{sum(1 for o in outcomes if o.outcome == 'no_finding')} 条如实报告找不到")
    print("\n  注：被丢弃和「找不到」的条目原样推送现有的确定性卡片 —— "
          "补齐只做加法，不替换任何既有输出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
