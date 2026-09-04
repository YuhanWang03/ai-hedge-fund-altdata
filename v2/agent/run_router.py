"""PyCharm / CLI 入口：给路由层打分，零 API key。

样例集里每条 query 都标了「分类器实际返回的 intent」和「应该走哪条路径」，
所以打分完全不需要调模型——这也意味着它可以进 CI，每次改信号表都跑一遍。

输出三件事：
1. 三种 mode 下的路由准确率（off / unknown_only / heuristic）
2. 每个信号命中了多少条、误判了多少条 —— 用来决定哪条规则该收紧
3. 逐条错误清单 —— 这才是真正能拿去调规则的东西
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from collections import Counter  # noqa: E402

from v2.agent import session  # noqa: E402
from v2.agent.router import ROUTING_MODES, route  # noqa: E402
from v2.agent.samples import CASES, PRONOUN_CASES  # noqa: E402

_RULE = "─" * 78


def score(mode: str) -> tuple[int, list[tuple], Counter, Counter]:
    """Return (correct, misses, signal hits, signal misses) for one mode."""
    correct = 0
    misses: list[tuple] = []
    hits: Counter = Counter()
    wrong: Counter = Counter()

    for case in CASES:
        parsed = {"intent": case.intent, "ticker": case.ticker}
        decision = route(case.query, parsed, mode=mode)

        # In "off" mode nothing but slash can differ, so the fair expectation is
        # "slash stays slash, everything else takes the fast path".
        # The explicit /ask escape is honoured in every mode by design, so it is
        # not remapped below — treating it as a miss would be a scoring bug.
        expected = case.expected
        explicit = case.query.lower().startswith("/ask")
        if expected == "agent" and not explicit:
            if mode == "off":
                expected = "single_hop"
            elif mode == "unknown_only" and case.intent != "unknown":
                expected = "single_hop"

        if decision.path == expected:
            correct += 1
            if decision.signal:
                hits[decision.signal] += 1
        else:
            misses.append((case, decision, expected))
            wrong[decision.signal or "(no signal)"] += 1

    return correct, misses, hits, wrong


def main() -> int:
    total = len(CASES)
    print(f"路由样例集：{total} 条\n")

    print(f"{_RULE}\n【各 mode 的路由准确率】\n{_RULE}")
    print(f"  {'mode':<16}{'正确':>8}{'总数':>8}{'准确率':>10}")
    results = {}
    for mode in ROUTING_MODES:
        correct, misses, hits, wrong = score(mode)
        results[mode] = (correct, misses, hits, wrong)
        print(f"  {mode:<16}{correct:>8}{total:>8}{correct / total:>9.0%}")

    correct, misses, hits, wrong = results["heuristic"]

    print(f"\n{_RULE}\n【heuristic 模式下各信号的表现】\n{_RULE}")
    print(f"  {'信号':<20}{'正确命中':>10}{'导致误判':>10}")
    for name in sorted(set(hits) | set(wrong)):
        print(f"  {name:<20}{hits.get(name, 0):>10}{wrong.get(name, 0):>10}")
    agent_routed = sum(hits.values())
    print(f"\n  进 agent 的比例：{agent_routed}/{total} = {agent_routed / total:.0%}"
          f"   （其余走单跳，成本约为 agent 的 1/10）")

    if misses:
        print(f"\n{_RULE}\n【heuristic 模式的错误清单 — 调规则就看这里】\n{_RULE}")
        for case, decision, expected in misses:
            print(f"  ✗ {case.query}")
            print(f"      intent={case.intent}  期望={expected}  实际={decision.path}"
                  f"  信号={decision.signal or '(none)'}")
            if case.note:
                print(f"      标注理由：{case.note}")
    else:
        print("\n  heuristic 模式无错误。")

    # -- pronoun resolution ---------------------------------------------------
    print(f"\n{_RULE}\n【指代消解】\n{_RULE}")
    store = session.SessionStore()
    passed = 0
    for chat_id, (first, follow_up, expected) in enumerate(PRONOUN_CASES):
        store.clear(chat_id)
        store.record(chat_id, session.Turn(
            query=first, tickers=tuple(session.extract_tickers(first))))
        resolution = store.resolve(chat_id, follow_up)
        actual = resolution.antecedent if resolution.rewritten else ""
        ok = actual == expected
        passed += ok
        arrow = resolution.text if resolution.rewritten else "（不改写）"
        print(f"  {'✓' if ok else '✗'} 「{first}」→「{follow_up}」  {arrow}")
    print(f"\n  {passed}/{len(PRONOUN_CASES)} 通过")

    return 0 if not misses and passed == len(PRONOUN_CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
