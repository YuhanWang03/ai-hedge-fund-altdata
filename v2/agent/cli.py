"""Side-by-side runner: same query, single-hop router vs agent loop.

    python -m v2.agent.cli "我持仓里哪只最危险" --mode both --tools fixture

``--tools fixture`` serves recorded observations, so the only thing the run
costs is the LLM call — which is the point: anyone can clone the repo and see
both control flows on the same question without a data-provider subscription.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap

from v2.agent.baseline import run_baseline
from v2.agent.llm import build_llm, describe_provider
from v2.agent.loop import AgentConfig, run_agent
from v2.agent.registry import ToolRegistry

_RULE = "─" * 72


def _load_dotenv() -> None:
    """Load the repo's .env when python-dotenv is available.

    Matches how the web backend bootstraps itself. Optional on purpose: the demo
    and the test suite must keep working in an environment with no third-party
    packages installed, so a missing dotenv is not an error.
    """
    try:
        import pathlib

        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")


def _wrap(text: str, indent: str = "    ", width: int = 96) -> str:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        lines.extend(textwrap.wrap(raw, width=width, subsequent_indent=indent) or [""])
    return "\n".join(indent + line if not line.startswith(indent) else line for line in lines)


def _print_baseline(result) -> None:
    print(f"\n{_RULE}\n【基线】单跳路由 — 复刻 v2/bot/commands.py:cmd_nl\n{_RULE}")
    print(f"  intent 分类 → {result.intent or '(none)'}")
    print(f"  调用工具   → {result.tool or '(none)'}  ×1")
    print("\n  回答（工具返回原文，LLM 不参与撰写）：")
    print(_wrap(result.answer[:1200], "    "))


def _print_agent(result) -> None:
    print(f"\n{_RULE}\n【Agent】模型驱动的多步循环\n{_RULE}")
    last_index = len(result.trajectory.steps) - 1
    for step in result.trajectory.steps:
        head = f"  步骤 {step.index + 1}"
        if step.tool_calls:
            names = ", ".join(
                f"{c.name}({', '.join(f'{k}={v}' for k, v in (c.arguments or {}).items())})"
                for c in step.tool_calls
            )
            print(f"{head}  调用 {len(step.tool_calls)} 个工具 → {names}")
            for call_result in step.results:
                mark = "✓" if call_result.ok else "✗"
                preview = call_result.content.replace("\n", " ")[:88]
                print(f"          {mark} {call_result.name:<20} {preview}")
        elif step.index == last_index:
            print(f"{head}  给出最终回答")
        else:
            print(f"{head}  给出回答 → grounding 检查未通过，退回重写")
        if step.response.text and step.tool_calls:
            print(f"          思考: {step.response.text.strip()[:160]}")

    if result.repairs:
        flagged = ", ".join(result.repaired_figures[:6]) or "—"
        print(f"\n  ⚠️ 触发 {result.repairs} 次 grounding 修复：初稿里 {flagged} "
              f"在任何工具返回中都找不到，已退回重写")
        print(f"     重写后：{result.grounding.summary()}")

    print("\n  最终回答：")
    print(_wrap(result.answer, "    "))


def _print_comparison(baseline, agent) -> None:
    b, a = baseline.stats(), agent.stats()
    rows = [
        ("LLM 调用次数",    b["llm_calls"],        a["llm_calls"]),
        ("工具调用次数",    b["tool_calls"],       a["tool_calls"]),
        ("不同工具数",      b["distinct_tools"],   a["distinct_tools"]),
        ("工具失败次数",    b["failed_tool_calls"], a["failed_tool_calls"]),
        ("耗时 (ms)",       b["elapsed_ms"],       a["elapsed_ms"]),
        ("总 token",        "—",                   a.get("total_tokens", 0)),
        ("数字溯源率",      f"{b['grounding_ratio']:.0%}", f"{a['grounding_ratio']:.0%}"),
        ("上下文压缩(字符)", "—",                   a.get("context_chars_saved", 0)),
        ("终止原因",        b["stop_reason"],      a["stop_reason"]),
    ]
    print(f"\n{_RULE}\n【对比】\n{_RULE}")
    print(f"  {'指标':<18}{'基线(单跳)':>16}{'Agent(多步)':>16}")
    for label, left, right in rows:
        print(f"  {label:<18}{str(left):>16}{str(right):>16}")
    print("\n  注：基线的溯源率恒为 100%，因为它原样返回工具输出、LLM 不撰写任何文字。")
    print("      代价是它只能回答单个工具就能回答的问题。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m v2.agent.cli",
        description="Compare the single-hop router against the agent loop.",
    )
    parser.add_argument("query", nargs="?", default="",
                        help="natural-language question")
    parser.add_argument("--demo", action="store_true",
                        help="replay a recorded trajectory — no API key of any kind")
    parser.add_argument("--mode", choices=["agent", "baseline", "both"], default="both")
    parser.add_argument("--tools", choices=["live", "fixture"], default="fixture",
                        help="live = real responders (needs data keys); "
                             "fixture = recorded observations (LLM cost only)")
    parser.add_argument("--allow-mutations", action="store_true",
                        help="permit tools that write to state.db")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--no-parallel", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit machine-readable stats")
    args = parser.parse_args(argv)
    _load_dotenv()

    if args.demo:
        from v2.agent.demo import DEMO_QUERY
        args.query = args.query or DEMO_QUERY
        args.tools = "fixture"
    if not args.query:
        parser.error("a query is required (or pass --demo)")

    if args.tools == "fixture":
        from v2.agent.fixtures import build_registry
        registry = build_registry(allow_mutations=args.allow_mutations)
    else:
        registry = ToolRegistry(allow_mutations=args.allow_mutations)

    config = AgentConfig(
        max_steps=args.max_steps,
        parallel=not args.no_parallel,
        allow_mutations=args.allow_mutations,
    )

    print(f"问题: {args.query}")
    if args.demo:
        print("模型: scripted（回放录制轨迹，零 API key）   工具层: fixture")
    else:
        print(f"模型: {describe_provider()}   工具层: {args.tools}")

    classifier = llm = None
    if args.demo:
        from v2.agent.demo import demo_classifier, demo_llm
        classifier, llm = demo_classifier, demo_llm()

    baseline = agent = None
    if args.mode in ("baseline", "both"):
        baseline = run_baseline(args.query, classifier=classifier, registry=registry)
        _print_baseline(baseline)
    if args.mode in ("agent", "both"):
        agent = run_agent(args.query, llm=llm or build_llm(), registry=registry, config=config)
        _print_agent(agent)
    if baseline and agent:
        _print_comparison(baseline, agent)

    if args.json:
        payload = {"query": args.query}
        if baseline:
            payload["baseline"] = baseline.stats()
        if agent:
            payload["agent"] = agent.stats()
        print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
