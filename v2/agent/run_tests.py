"""PyCharm 入口：跑 v2/agent 的测试，不需要 pytest。

为什么不直接运行 test_agent.py：PyCharm 看到 test_ 开头的文件名会自动切到 pytest
runner，而 pytest 插件依赖 pkg_resources（setuptools），项目的 v2/conftest.py 又
依赖 python-dotenv。一个只装了 LLM 客户端的干净环境两样都没有，于是还没跑到测试
就 collection error。这个文件不叫 test_*，PyCharm 会当普通脚本执行，走
test_agent.py 里那个纯 stdlib 的 runner，零依赖。

装了完整依赖的话，`pytest v2/agent/` 一样可以跑，两条路径跑的是同一批测试函数。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import traceback  # noqa: E402

from v2.agent import test_agent  # noqa: E402


def main() -> int:
    tests = [(name, obj) for name, obj in sorted(vars(test_agent).items())
             if name.startswith("test_") and callable(obj)]
    failures: list[str] = []

    print(f"运行 {len(tests)} 个测试 — {test_agent.__file__}\n")
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception:
            failures.append(name)
            print(f"  ✗ {name}")
            traceback.print_exc()

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        print("失败：" + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
