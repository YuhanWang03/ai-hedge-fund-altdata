"""PyCharm 入口：用真实模型跑对比，只花 LLM 的钱。

和 run_demo.py 的区别：那个是回放录制轨迹，这个是让模型当场决定调什么工具。
工具层默认仍用录制观测（TOOLS = "fixture"），所以不需要 Financial Datasets /
Alpaca 这些数据源的 key，只需要一个 LLM key。

key 从仓库根目录的 .env 读取（CLI 会自动加载），或直接设成系统环境变量：
    DEEPSEEK_API_KEY=sk-...
换 provider 就改 AGENT_LLM_BASE_URL / AGENT_LLM_MODEL / AGENT_LLM_API_KEY。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent.cli import main  # noqa: E402


# ---------------------------------------------------------------------------
# 运行配置：环境变量优先，其次是下面的默认值。
#
# **改配置优先用环境变量，别直接改这个文件** —— 它被 git 跟踪，本地一改，
# 下次 git pull 会中止在 "local changes would be overwritten"。
#   AGENT_QUERY / AGENT_MODE / AGENT_TOOLS / AGENT_MAX_STEPS
# ---------------------------------------------------------------------------

import os  # noqa: E402

QUERY = os.environ.get("AGENT_QUERY", "").strip() or "我持仓里哪只最危险？"
# 其他适合看多跳的问题：
#   "我这周为什么亏钱？"
#   "我的持仓里有哪些快发财报了，风险大不大？"
#   "SMCI 最近出什么事了？"

MODE = os.environ.get("AGENT_MODE", "").strip() or "both"   # both | agent | baseline
TOOLS = os.environ.get("AGENT_TOOLS", "").strip() or "fixture"
                       # fixture = 录制观测，只需 LLM key
                       # live    = 打真实数据源，需要 poetry install + 全套 key
try:
    MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "") or 8)
except ValueError:
    MAX_STEPS = 8
ALLOW_MUTATIONS = False   # True 才允许写库的 4 个工具（watchlist / alert 增删）
SHOW_JSON = False


if __name__ == "__main__":
    argv = [QUERY, "--mode", MODE, "--tools", TOOLS, "--max-steps", str(MAX_STEPS)]
    if ALLOW_MUTATIONS:
        argv.append("--allow-mutations")
    if SHOW_JSON:
        argv.append("--json")
    sys.exit(main(argv))
