"""PyCharm / CLI 入口：B2 的 MD&A 解读层演示与打分，零 API key。

四个标的刻意覆盖四种结局：
  CRWD  → 解读成功（两段有实质信息，引用逐字可查）
  AAPL  → 新增段落全是会计政策模板，如实返回空
  BADCO → 引用是编造的，整条被拒
  MSFT  → 本季没有新增段落，一次调用都不发生
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import json  # noqa: E402

from v2.agent import mda_reader  # noqa: E402
from v2.agent.llm import LLMResponse, ScriptedLLM  # noqa: E402
from v2.agent.samples import MDA_CASES  # noqa: E402

_RULE = "─" * 78


def _says(payload) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload, ensure_ascii=False),
                       prompt_tokens=1200, completion_tokens=180)


SCRIPTS = {
    "CRWD": _says({"points": [
        {"quote": "extended customer acceptance cycles",
         "reading": "企业客户签约到确认收入的周期在拉长，金融行业尤其明显，"
                    "对后续几个季度的收入确认节奏是压力",
         "direction": "利空"},
        {"quote": "a charge of $18.4 million related to the restructuring of",
         "reading": "EMEA 销售组织重组已计提 $18.4 million，且后续还有最多 $6.0 million，"
                    "说明该区域的销售模式在调整而非小修小补",
         "direction": "利空"},
    ]}),
    "AAPL": _says({"points": []}),
    "BADCO": _says({"points": [
        # 引用听起来很像原文，但一个字都不在里面 —— 这正是要拦的东西
        {"quote": "management expects liquidity to normalize by year end",
         "reading": "管理层预计年底流动性恢复正常",
         "direction": "利好"},
    ]}),
}


def main() -> int:
    print(f"{_RULE}\n【B2】10-Q MD&A 解读层\n{_RULE}")
    print("  确定性部分（新增段落数 / 新增风险因素 / going concern / 重大缺陷）")
    print("  始终按现状推送，下面这层只做加法。\n")

    total_tokens = 0
    produced = rejected = 0

    for ticker, delta in MDA_CASES.items():
        script = SCRIPTS.get(ticker)
        llm = ScriptedLLM([script]) if script else ScriptedLLM([])
        outcome = mda_reader.read(delta, llm=llm)
        total_tokens += outcome.tokens
        produced += len(outcome.points)
        rejected += len(outcome.rejected)

        mark = {"ok": "✅", "no_finding": "🟡", "nothing_to_read": "⚪",
                "unquoted": "❌", "ungrounded": "❌", "unparsable": "❌",
                "timeout": "⏱", "error": "❌"}.get(outcome.outcome, "·")
        deterministic = (
            f"新增段落 {len(delta.mda_added_paragraphs)} · "
            f"新增风险因素 {delta.new_risk_factor_count}"
            + ("  ⚠️ going concern" if delta.has_going_concern else "")
        )
        print(f"  {mark} {ticker:<6} {outcome.outcome:<16} {outcome.tokens:>5} token"
              f"   [确定性部分：{deterministic}]")
        if outcome.detail:
            print(f"       {outcome.detail}")
        for note in outcome.rejected:
            print(f"       ⛔ {note}")
        if outcome.ok:
            for line in outcome.render().splitlines()[1:]:
                print(f"       {line}")
        print()

    print(f"{_RULE}\n【效果】\n{_RULE}")
    print(f"  产出解读       {produced} 条")
    print(f"  拒绝           {rejected} 条（引用无法在原文中逐字找到）")
    print(f"  零成本跳过     {sum(1 for d in MDA_CASES.values() if not d.mda_added_paragraphs)} 个"
          f"（本季无新增段落，一次调用都没发生）")
    print(f"  代价           {total_tokens} token")
    print("\n  注：被拒和「无实质信号」的标的，卡片与现在完全一致 —— "
          "解读层失败时用户看不出任何区别。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
