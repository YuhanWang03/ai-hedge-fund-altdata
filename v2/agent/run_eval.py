"""PyCharm / CLI 入口：跑评测套件。

  python3 -m v2.agent.run_eval                       # 有 LLM key：baseline / routed / agent
  python3 -m v2.agent.run_eval --modes baseline      # 零 key 也能跑（基线不需要模型）
  python3 -m v2.agent.run_eval --modes baseline routed agent agent_no_repair
  python3 -m v2.agent.run_eval --category ranking --workers 8 --out eval.json

基线那一档完全不需要 API key —— 它用样例上标注的 intent 直接分发，工具层是录制观测。
所以任何人 clone 下来都能立刻看到「现有系统在这 83 条上得几分」，这本身就是对比的起点。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from v2.agent.eval import runner  # noqa: E402
from v2.agent.eval.cases import CASES, CATEGORIES  # noqa: E402
from v2.agent.llm import OpenAICompatLLM, build_llm, describe_provider  # noqa: E402


def _needs_llm(modes: list[str]) -> bool:
    return any(runner.MODES[m].kind != "baseline" for m in modes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m v2.agent.run_eval")
    parser.add_argument("--modes", nargs="+", default=list(runner.DEFAULT_MODES),
                        choices=sorted(runner.MODES))
    parser.add_argument("--category", nargs="+", choices=sorted(CATEGORIES))
    parser.add_argument("--limit", type=int, help="只跑前 N 条（快速冒烟）")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", help="把逐条结果写成 JSON")
    parser.add_argument("--failures", type=int, default=20, help="打印多少条失败明细")
    args = parser.parse_args(argv)

    # Load .env the same way the comparison CLI does.
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        pass

    modes = list(dict.fromkeys(args.modes))
    if _needs_llm(modes) and not OpenAICompatLLM().api_key:
        keep = [m for m in modes if runner.MODES[m].kind == "baseline"]
        print("⚠️  没有找到 LLM API key —— 只跑不需要模型的 baseline 档。\n"
              "    配好 DEEPSEEK_API_KEY 后再跑 routed / agent 才有对比。\n")
        modes = keep or ["baseline"]

    cases = CASES
    if args.category:
        wanted = set(args.category)
        cases = tuple(c for c in cases if c.category in wanted)
    if args.limit:
        cases = cases[: args.limit]

    print(f"评测集：{len(cases)} 条 · 模式：{', '.join(modes)}")
    if _needs_llm(modes):
        print(f"模型：{describe_provider()}")
    print()

    done = 0
    lock = threading.Lock()
    total_runs = len(cases) * len(modes)

    def _progress(score) -> None:
        nonlocal done
        with lock:
            done += 1
            mark = "✓" if score.passed else "✗"
            print(f"\r  [{done}/{total_runs}] {mark} {score.mode}/{score.case_id}"
                  .ljust(60), end="", flush=True)

    reports = []
    started = time.time()
    for mode in modes:
        workers = 1 if runner.MODES[mode].kind == "baseline" else args.workers
        reports.append(runner.run_suite(mode, llm_factory=build_llm, cases=cases,
                                        workers=workers, on_case=_progress))
    print("\r".ljust(70) + f"\r完成，用时 {time.time() - started:.1f}s\n")

    print(runner.render_comparison(reports))
    print()
    print(runner.render_categories(reports))
    for report in reports:
        if report.mode == "routed":
            print()
            print(runner.render_routing(report))
    print()
    print(runner.render_failures(reports[-1], limit=args.failures))

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(runner.to_json(reports), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n逐条结果已写入 {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
