"""PyCharm 入口：直接点绿三角运行，零 API key。

为什么单独放一个文件：在 IDE 里右键运行某个模块时，Python 把**该文件所在目录**
放进 sys.path[0]（这里是 v2/agent/），而不是仓库根目录，于是 `import v2.agent`
会 ModuleNotFoundError。下面三行把仓库根目录补进去，所以这个文件在任何工作目录、
任何 IDE 配置下都能直接运行。

想换问题或换模式，改下面的常量即可，不用碰命令行。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from v2.agent.cli import main  # noqa: E402  (必须在 sys.path 修好之后)


# ---------------------------------------------------------------------------
# 改这里
# ---------------------------------------------------------------------------

QUERY = "我持仓里哪只最危险？"   # --demo 模式下这个问题固定，改了也是回放同一条轨迹
MODE = "both"                    # "both" | "agent" | "baseline"
SHOW_JSON = False                # True 则额外打印机器可读的 stats


if __name__ == "__main__":
    argv = ["--demo", "--mode", MODE]
    if SHOW_JSON:
        argv.append("--json")
    sys.exit(main(argv))
