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
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from v2.agent.eval import runner  # noqa: E402
from v2.agent.eval.cases import CASES, CATEGORIES  # noqa: E402
from v2.agent.llm import OpenAICompatLLM, build_llm, describe_provider  # noqa: E402


# ---------------------------------------------------------------------------
# 运行配置
#
# 优先级：命令行参数 > 环境变量 > 下面的默认值。
#
# **想改配置请用环境变量或命令行，不要直接改这个文件** —— 它被 git 跟踪，本地一改，
# 下次 git pull 就会以 "local changes would be overwritten by merge" 中止。这个
# 中止很容易被忽略，后果是你以为在跑新代码、其实跑的是旧的（这件事真实发生过）。
#
# PyCharm 里设环境变量：Run → Edit Configurations → Environment variables，
# 填分号分隔的串，例如 EVAL_REPEAT=3;EVAL_WORKERS=8
#
#   EVAL_MODES=baseline routed agent   跑哪几档
#   EVAL_REPEAT=3                      每条跑几次（区分真失败与抖动）
#   EVAL_WORKERS=8                     并发数
#   EVAL_LIMIT=10                      只跑前 N 条，快速冒烟
#   EVAL_CATEGORY=ranking multi_hop    只跑某几类
#   EVAL_CASES=c01,c02,m07             只跑指定 case（配 EVAL_REPEAT 用来看抖动）
#
#   EVAL_OUT=data/eval.json            JSON 输出路径
#
#   看抖动的标准跑法（6 条 × 10 次 ≈ 一次全量的 2/3 成本）：
#     EVAL_MODES=production EVAL_CASES=c01,d06,k03,m07,r09,t04 EVAL_REPEAT=10 \
#       EVAL_OUT=data/flaky.json poetry run python v2/agent/run_eval.py
#   输出里的「抖动分叉点」会把每条按 error / budget / tool_choice / wording 分类。
#   变量和命令必须在同一行（或先 export）；VPS 上要用 poetry run，系统 python3
#   没装 python-dotenv，.env 里的 key 读不到。
# ---------------------------------------------------------------------------


def _env_str(name: str, default):
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default):
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_list(name: str, default):
    raw = os.environ.get(name, "").strip()
    return raw.replace(",", " ").split() if raw else default


# One default, owned by the runner. This used to be a second hardcoded list,
# and the two drifted the first time the runner's changed: a `production` mode
# was added to runner.DEFAULT_MODES, the sweep was run to measure it, and the
# header said «baseline, routed, agent» — the whole run answered nothing.
MODES = _env_list("EVAL_MODES", list(runner.DEFAULT_MODES))
WORKERS = _env_int("EVAL_WORKERS", 4)
# data/ 已在 .gitignore:31 —— 评测产物不会被误提交
OUT = _env_str("EVAL_OUT", "data/eval.json")
CATEGORY = _env_list("EVAL_CATEGORY", None)
ONLY = _env_list("EVAL_CASES", None)
LIMIT = _env_int("EVAL_LIMIT", None)
FAILURES = _env_int("EVAL_FAILURES", 20)
REPEAT = _env_int("EVAL_REPEAT", 1)


def _needs_llm(modes: list[str]) -> bool:
    return any(runner.MODES[m].kind != "baseline" for m in modes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m v2.agent.run_eval")
    parser.add_argument("--modes", nargs="+", default=list(MODES),
                        choices=sorted(runner.MODES))
    parser.add_argument("--category", nargs="+", default=CATEGORY,
                        choices=sorted(CATEGORIES))
    parser.add_argument("--cases", nargs="+", default=ONLY, metavar="ID",
                        help="只跑这些 case id —— 抖动要靠同一小批多跑几次来分辨，"
                             "而全量重复十次太贵")
    parser.add_argument("--limit", type=int, default=LIMIT, help="只跑前 N 条（快速冒烟）")
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--out", default=OUT, help="把逐条结果写成 JSON")
    parser.add_argument("--failures", type=int, default=FAILURES,
                        help="打印多少条失败明细")
    parser.add_argument("--repeat", type=int, default=REPEAT,
                        help="每条 case 跑几次，用于区分真失败与抖动")
    args = parser.parse_args(argv)

    # Load .env the same way the comparison CLI does. If python-dotenv is not
    # importable the file is silently never read — which is exactly what
    # happens under the system `python3` on the VPS, where only the poetry
    # venv has the package. A 6×10 flaky sweep once ran as a 0-second
    # baseline because of this, and the message blamed the key.
    dotenv_state = "loaded"
    try:
        from dotenv import load_dotenv
        load_dotenv(_REPO_ROOT / ".env")
    except ImportError:
        dotenv_state = "python-dotenv 未安装，.env 没有被读取"

    modes = list(dict.fromkeys(args.modes))
    if _needs_llm(modes) and not OpenAICompatLLM().api_key:
        keep = [m for m in modes if runner.MODES[m].kind == "baseline"]
        env_file = _REPO_ROOT / ".env"
        hint = (f"    .env：{'存在' if env_file.exists() else '不存在'}（{env_file}）"
                f" · dotenv：{dotenv_state} · 解释器：{sys.executable}")
        print("⚠️  没有找到 LLM API key —— 只跑不需要模型的 baseline 档。\n"
              f"{hint}\n"
              "    key 在 .env 里而 dotenv 未安装时，用 `poetry run python v2/agent/run_eval.py`"
              "，或先 `set -a; source .env`。\n")
        modes = keep or ["baseline"]

    cases = CASES
    if args.cases:
        wanted = {c.strip() for c in args.cases}
        unknown = wanted - {c.id for c in CASES}
        if unknown:
            parser.error(f"没有这些 case：{', '.join(sorted(unknown))}")
        cases = tuple(c for c in cases if c.id in wanted)
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
    agent_modes = sum(1 for m in modes if runner.MODES[m].kind != "baseline")
    baseline_modes = len(modes) - agent_modes
    total_runs = len(cases) * (agent_modes * max(args.repeat, 1) + baseline_modes)

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
                                        workers=workers, repeat=args.repeat,
                                        on_case=_progress))
    print("\r".ljust(70) + f"\r完成，用时 {time.time() - started:.1f}s\n")

    print(runner.render_comparison(reports))
    print()
    print(runner.render_categories(reports))
    for report in reports:
        if report.mode == "routed":
            print()
            print(runner.render_routing(report))
    for report in reports:
        if report.ungrounded_breakdown():
            print()
            print(runner.render_grounding(report))
    # Printed unconditionally: "no false positives" is the result worth seeing,
    # and a section that only appears when something is wrong trains you to stop
    # looking for it.
    for report in reports:
        if report.mode != "baseline":
            print()
            print(runner.render_attribution(report))
    print()
    print(runner.render_stability(reports[-1]))
    if reports[-1].repeat > 1:
        print()
        print(runner.render_divergence(reports[-1]))
    print()
    print(runner.render_overspend(reports[-1]))
    print()
    print(runner.render_failures(reports[-1], limit=args.failures))

    if args.out:
        out_path = pathlib.Path(args.out)
        if not out_path.is_absolute():
            out_path = _REPO_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(runner.to_json(reports), ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n逐条结果已写入 {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
